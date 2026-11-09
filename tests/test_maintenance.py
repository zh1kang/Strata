"""Optimistic rebuilds, leases, failures, and scheduler behaviour."""

from __future__ import annotations

import pytest
from conftest import seed_events

from strata import (
    Budget,
    Context,
    LevelViolationError,
    Memory,
    NodeState,
    NotFoundError,
    Policy,
)


def state(mem: Memory, kind: str, key: str) -> str:
    return mem.node(kind, key).state


# ---------------------------------------------------------------- optimistic safety


def test_input_change_during_rebuild_discards_result(mem: Memory, other: Memory) -> None:
    mem.assert_fact("a", "v", 1, "e0")
    fired = []

    def slow(ctx: Context) -> int:
        value = ctx.slot("a", "v")
        if not fired:  # a concurrent writer changes the input mid-computation
            fired.append(True)
            other.assert_fact("a", "v", 2, "e1")
        return value

    mem.define("n", "a", 1, slow, cost=2.0)
    first = mem.maintain(10)
    assert (first.jobs, first.conflicts, first.wasted_cost, first.cost) == (0, 1, 2.0, 2.0)
    assert first.outcomes == ["n:a:conflict"]
    assert state(mem, "n", "a") != NodeState.VALID  # the stale value was never committed
    assert mem.node("n", "a").value is None

    second = mem.maintain(10)
    assert second.jobs == 1
    assert mem.node("n", "a").value == 2


def test_upstream_node_change_during_downstream_rebuild_conflicts(
    mem: Memory, other: Memory
) -> None:
    mem.assert_fact("a", "v", 1, "e0")
    mem.define("lo", "a", 1, lambda c: c.slot("a", "v"))
    mem.maintain(10)
    fired = []

    def hi(ctx: Context) -> int:
        value = ctx.node("lo", "a")
        if not fired:
            fired.append(True)
            other.assert_fact("a", "v", 2, "e1")  # invalidates lo, hence hi
        return value

    mem.define("hi", "a", 2, hi)
    mem.assert_fact("a", "v", 1, "e2")  # same value: no invalidation, hi still builds
    report = mem.maintain(10)
    assert report.conflicts == 1
    assert state(mem, "hi", "a") != NodeState.VALID
    mem.maintain(10)
    assert mem.node("hi", "a").value == 2


def test_lease_held_elsewhere_blocks_rebuild_until_expiry(db_path: str) -> None:
    now = [1000.0]
    with Memory.open(db_path, clock=lambda: now[0], lease_ttl=30) as m:
        m.define("n", "a", 1, lambda c: 1)
        node_id = m.engine.node_id("n", "a")
        m.store.db.execute(
            "INSERT INTO leases(node_id, token, expires_at) VALUES (?, 'other-worker', ?)",
            (node_id, now[0] + 30),
        )
        busy = m.maintain(10)
        assert (busy.jobs, busy.lease_busy, busy.cost) == (0, 1, 0.0)
        now[0] += 31  # lease expired: it can be taken over
        report = m.maintain(10)
        assert report.jobs == 1
        assert m.store.db.execute("SELECT count(*) FROM leases").fetchone()[0] == 0


def test_lost_lease_discards_result(db_path: str) -> None:
    with Memory.open(db_path) as m:

        def fn(ctx: Context) -> int:
            # another worker steals the lease while we compute
            m.store.db.execute("UPDATE leases SET token='thief'")
            return 1

        m.define("n", "a", 1, fn)
        report = m.maintain(10)
        assert (report.jobs, report.conflicts, report.wasted_cost) == (0, 1, 1.0)
        assert report.outcomes == ["n:a:lease_lost"]
        assert m.node("n", "a").value is None


def test_failure_retries_then_fails_and_can_be_retried(mem: Memory) -> None:
    broken = [True]

    def flaky(ctx: Context) -> str:
        if broken[0]:
            raise RuntimeError("boom")
        return "ok"

    mem.define("n", "a", 1, flaky)
    mem.define("downstream", "a", 2, lambda c: c.node("n", "a"))
    mem.define("healthy", "a", 1, lambda c: "fine")
    for attempt in (1, 2):
        report = mem.maintain(10)
        assert report.failures == 1
        assert mem.node("n", "a").attempts == attempt
        assert state(mem, "n", "a") == NodeState.PENDING
    mem.maintain(10)
    node = mem.node("n", "a")
    assert node.state == NodeState.FAILED
    assert "RuntimeError: boom" in node.last_error
    assert mem.node("healthy", "a").value == "fine"  # one failure does not stop the rest
    assert mem.node("downstream", "a").value is None

    broken[0] = False
    assert mem.maintain(10).jobs == 0  # failed nodes stay parked
    assert mem.retry_failed() == 1
    mem.maintain(10)
    assert mem.node("downstream", "a").value == "ok"
    assert mem.node("n", "a").last_error is None


def test_failed_attempt_costs_budget_but_is_reported_as_waste(mem: Memory) -> None:
    def bad(ctx: Context) -> None:
        raise ValueError("nope")

    mem.define("n", "a", 1, bad, cost=3.0)
    report = mem.maintain(10)
    assert (report.cost, report.wasted_cost, report.failures, report.jobs) == (3.0, 3.0, 1, 0)


def test_unserializable_result_is_a_failure(mem: Memory) -> None:
    mem.define("n", "a", 1, lambda c: {1, 2})
    assert mem.maintain(10).failures == 1


def test_level_violation_is_raised_and_node_parked(mem: Memory) -> None:
    mem.define("high", "x", 2, lambda c: 1)
    mem.define("low", "x", 1, lambda c: c.node("high", "x"))
    with pytest.raises(LevelViolationError):
        mem.maintain(10)
    assert state(mem, "low", "x") == NodeState.FAILED
    assert mem.store.db.execute("SELECT count(*) FROM leases").fetchone()[0] == 0
    with pytest.raises(LevelViolationError):  # same level is also a violation
        mem.define("same", "x", 1, lambda c: c.node("low", "x"))
        mem.maintain(10)


def test_swallowed_not_ready_signal_cannot_commit_a_dependency_free_result(mem: Memory) -> None:
    mem.assert_fact("k", "v", 1, "e0")
    mem.define("low", "k", 1, lambda c: c.slot("k", "v"))

    def high(ctx: Context) -> dict[str, object]:
        try:
            return {"low": ctx.node("low", "k")}
        except Exception:  # a broad handler must not hide that the input was not valid
            return {"low": "fallback"}

    mem.define("high", "k", 2, high)
    report = mem.engine.maintain(10, only=[mem.engine.node_id("high", "k")])
    # The swallowed signal is re-raised; the restricted run then builds the input it lacked.
    assert report.outcomes == ["high:k:not_ready", "low:k:computed", "high:k:computed"]
    assert mem.node("high", "k").value == {"low": 1}
    mem.assert_fact("k", "v", 2, "e1")
    assert mem.read_node("high", "k").status == "unavailable"
    mem.maintain(10)
    assert mem.node("high", "k").value == {"low": 2}


def test_swallowed_level_violation_still_parks_the_node(mem: Memory) -> None:
    mem.define("high", "x", 2, lambda c: 1)

    def low(ctx: Context) -> str:
        try:
            return ctx.node("high", "x")
        except LevelViolationError:
            return "ignored"

    mem.define("low", "x", 1, low)
    with pytest.raises(LevelViolationError):
        mem.maintain(10)
    assert state(mem, "low", "x") == NodeState.FAILED


def test_unregistered_and_not_ready_nodes_do_not_block_others(db_path: str) -> None:
    with Memory.open(db_path) as m:
        m.define("ghost", "a", 1, lambda c: 1)
    with Memory.open(db_path) as m:  # new process: ghost has no registered function
        m.define("live", "a", 1, lambda c: 2)
        report = m.maintain(10)
        assert report.unregistered == 1 and report.jobs == 1
        assert m.node("live", "a").value == 2
        assert m.node("ghost", "a").state == NodeState.PENDING


def test_missing_dependency_is_failure_and_dirty_dependency_is_built_first(mem: Memory) -> None:
    mem.define("n", "a", 2, lambda c: c.node("nope", "a"))
    assert mem.maintain(10).failures == 1  # a missing dependency is an ordinary failure
    assert "NotFoundError" in mem.node("n", "a").last_error

    mem.define("dep", "a", 1, lambda c: 1)  # pending, never built
    mem.define("reader", "a", 2, lambda c: c.node("dep", "a"))
    report = mem.maintain(10)
    assert mem.node("reader", "a").value == 1
    assert report.outcomes.index("dep:a:computed") < report.outcomes.index("reader:a:computed")


def test_unknown_node_lookup_raises(mem: Memory) -> None:
    with pytest.raises(NotFoundError):
        mem.node("x", "y")


# ---------------------------------------------------------------- scheduler


def computed(report) -> list[str]:
    return [o.split(":")[1] for o in report.outcomes if o.endswith(":computed")]


def test_fifo_orders_by_age_then_id(mem: Memory) -> None:
    for name in ("a", "b", "c"):
        mem.assert_fact(name, "v", 1, "e0")
        mem.define("n", name, 1, lambda ctx, n=name: ctx.slot(n, "v"))
    mem.maintain(None)
    for name in ("c", "a", "b"):  # dirtied in this order
        mem.assert_fact(name, "v", 2, "e1")
    report = mem.maintain(None, Policy.FIFO)
    assert computed(report) == ["c", "a", "b"]


def test_priority_prefers_hot_and_cheap_nodes(mem: Memory) -> None:
    for name, cost in (("cold", 1.0), ("hot", 1.0), ("pricey", 4.0)):
        mem.assert_fact(name, "v", 1, "e0")
        mem.define("n", name, 1, lambda ctx, n=name: ctx.slot(n, "v"), cost=cost)
    mem.maintain(None)
    for name in ("cold", "pricey", "hot"):
        mem.assert_fact(name, "v", 2, "e1")
    for _ in range(3):
        mem.read_node("n", "hot")  # accesses raise priority even while unavailable
    report = mem.maintain(None, Policy.PRIORITY)
    assert computed(report) == ["hot", "cold", "pricey"]
    # FIFO on the same dirty order would have started with "cold".


def test_budget_is_never_exceeded_and_oversized_nodes_are_reported(mem: Memory) -> None:
    for name, cost in (("a", 2.0), ("b", 5.0), ("c", 2.0)):
        mem.define("n", name, 1, lambda ctx, n=name: n, cost=cost)
    report = mem.maintain(Budget(4.5), Policy.FIFO)
    assert report.cost == 4.0 and report.jobs == 2  # b did not fit; a and c still ran (first-fit)
    assert report.skipped_budget == 1 and report.dirty == 1
    assert "n:b:skipped_budget" in report.outcomes
    follow_up = mem.maintain(Budget(4.5), Policy.FIFO)
    assert follow_up.jobs == 0 and follow_up.skipped_budget == 1  # 5.0 > 4.5 forever
    assert mem.maintain(Budget(5.0)).jobs == 1


def test_zero_budget_runs_nothing_but_verification_is_free(mem: Memory) -> None:
    mem.assert_fact("a", "name", "alpha", "e0")
    mem.define("size", "a", 1, lambda c: len(c.slot("a", "name")), cost=2.0)
    mem.define("label", "a", 2, lambda c: c.node("size", "a"), cost=2.0)
    mem.maintain(None)
    mem.assert_fact("a", "name", "omega", "e1")
    assert mem.maintain(Budget(0)).jobs == 0
    mem.maintain(Budget(2.0))  # size recomputes, same value -> label verified for free
    assert state(mem, "label", "a") == NodeState.VALID


def test_max_jobs_limits_work(mem: Memory) -> None:
    for name in "abc":
        mem.define("n", name, 1, lambda ctx, n=name: n)
    report = mem.maintain(Budget(100, max_jobs=2), Policy.FIFO)
    assert report.jobs == 2 and report.dirty == 1


def test_budget_validation_and_policy_validation(mem: Memory) -> None:
    with pytest.raises(ValueError):
        Budget(-1)
    with pytest.raises(ValueError):
        Budget(float("nan"))
    with pytest.raises(ValueError):
        mem.maintain(1, "lifo")
    with pytest.raises(ValueError):
        mem.define("n", "a", 0, lambda c: 1)
    with pytest.raises(ValueError):
        mem.define("n", "a", 1, lambda c: 1, cost=-1)
    mem.define("n", "a", 1, lambda c: 1)
    with pytest.raises(ValueError):
        mem.define("n", "a", 2, lambda c: 1)  # level is part of node identity


def test_scheduling_is_deterministic(tmp_path) -> None:
    def build(name: str) -> list[str]:
        with Memory.open(str(tmp_path / name)) as m:
            seed_events(m)
            for i in range(6):
                m.assert_fact(f"s{i}", "v", 1, "e0")
                m.define("n", f"s{i}", 1, lambda c, i=i: c.slot(f"s{i}", "v"), cost=1.0 + i % 3)
            m.maintain(None)
            for i in (4, 1, 5, 0, 3, 2):
                m.assert_fact(f"s{i}", "v", 2, "e1")
            outcomes: list[str] = []
            for policy in (Policy.PRIORITY, Policy.FIFO):
                outcomes += m.maintain(Budget(4), policy).outcomes
            outcomes += m.maintain(None).outcomes
            return outcomes

    assert build("one.db") == build("two.db")


def test_report_dirty_counts_remaining_work(mem: Memory) -> None:
    for name in "abc":
        mem.define("n", name, 1, lambda ctx, n=name: n)
    report = mem.maintain(Budget(1))
    assert (report.jobs, report.dirty, report.skipped_budget) == (1, 2, 2)
    assert mem.maintain(None).dirty == 0


def test_node_scheduled_before_its_dependency_is_deferred_not_failed(mem: Memory) -> None:
    mem.define("reader", "a", 2, lambda c: c.node("dep", "a") + 1)  # lower node id: tried first
    mem.define("dep", "a", 1, lambda c: 1)
    report = mem.maintain(10)
    assert report.not_ready == 1 and report.failures == 0
    assert report.cost == 2.0  # the deferral itself was free
    assert mem.node("reader", "a").value == 2
    assert mem.node("reader", "a").attempts == 0


def test_measured_cost_replaces_the_declared_cost(mem: Memory) -> None:
    mem.assert_fact("a", "v", 1, "e0")
    mem.assert_fact("b", "v", 1, "e0")

    def costly(ctx: Context) -> object:
        ctx.charge(3)
        ctx.charge(2.5)
        return ctx.slot(ctx.key, "v")

    mem.define("n", "a", 1, costly, cost=1.0)
    mem.define("n", "b", 1, lambda c: c.slot("b", "v"), cost=2.0)
    report = mem.maintain(None, Policy.FIFO)
    assert report.cost == 7.5  # 5.5 measured + 2 declared
    assert mem.node("n", "a").measured_cost == 5.5 and mem.node("n", "b").measured_cost is None

    mem.assert_fact("a", "v", 2, "e1")
    mem.assert_fact("b", "v", 2, "e1")
    report = mem.maintain(Budget(5))  # priority now prefers the cheaper b; a no longer fits
    assert report.outcomes == ["n:b:computed", "n:a:skipped_budget"]


def test_invalid_charge_fails_the_job(mem: Memory) -> None:
    mem.assert_fact("a", "v", 1, "e0")

    def bad(ctx: Context) -> object:
        ctx.charge(-1)
        return 1

    mem.define("n", "a", 1, bad)
    report = mem.maintain(10)
    assert report.failures == 1
    assert "ValueError" in (mem.node("n", "a").last_error or "")
