"""Conditional facts (premises, contingencies), demand scheduling, and the v3 migration."""

from __future__ import annotations

import pytest
from test_events_facts import downgrade_to_v3

from strata import Budget, Context, Event, FactWrite, Memory, Policy

FRAMEWORK = ("project", "framework")


def setup_build_tool(mem: Memory) -> None:
    mem.assert_facts(
        [
            FactWrite("project", "framework", "Synthari", "e0"),
            FactWrite("project", "build_tool", "Zelthra Pack", "e0", premises=(FRAMEWORK,)),
            FactWrite("project", "layout", "single-app", "e0", premises=(FRAMEWORK,)),
        ]
    )


def test_fact_is_unverified_once_its_premise_changes(mem: Memory) -> None:
    setup_build_tool(mem)
    assert mem.fact_status("project", "layout").status == "current"
    mem.assert_fact("project", "framework", "Veltrion", "e1")
    status = mem.fact_status("project", "layout")
    assert status.status == "unverified"
    assert status.reasons == ('project.framework changed from "Synthari" to "Veltrion"',)
    assert mem.get_fact("project", "layout").value == "single-app"  # the fact itself is kept
    mem.assert_fact("project", "layout", "monorepo", "e2")
    assert mem.fact_status("project", "layout").status == "current"  # restated without premise


def test_premise_restored_to_recorded_value_is_current_again(mem: Memory) -> None:
    setup_build_tool(mem)
    mem.assert_fact("project", "framework", "Veltrion", "e1")
    mem.assert_fact("project", "framework", "Synthari", "e2")
    assert mem.fact_status("project", "layout").status == "current"


def test_premises_in_one_batch_are_snapshotted_at_commit(mem: Memory) -> None:
    mem.assert_facts(
        [
            FactWrite("project", "build_tool", "Zelthra Pack", "e0", premises=(FRAMEWORK,)),
            FactWrite("project", "framework", "Synthari", "e0"),
        ]
    )
    assert mem.fact_status("project", "build_tool").status == "current"


def test_contingency_fires_in_the_transaction_that_changes_the_premise(mem: Memory) -> None:
    setup_build_tool(mem)
    mem.assert_facts(
        [FactWrite("project", "build_tool", "Quorix Forge", "e1", mode="contingent",
                   premises=(FRAMEWORK,))]
    )  # fmt: skip
    assert mem.get_fact("project", "build_tool").value == "Zelthra Pack"  # not yet
    mem.assert_fact("project", "framework", "Veltrion", "e2")
    fact = mem.get_fact("project", "build_tool")
    assert (fact.value, fact.source_event) == ("Quorix Forge", "e2")
    assert mem.fact_status("project", "build_tool").status == "current"
    assert mem.fact_status("project", "layout").status == "unverified"  # no rule for it
    mem.assert_fact("project", "framework", "Ombra", "e3")  # the contingency was used up
    assert mem.get_fact("project", "build_tool").value == "Quorix Forge"
    assert mem.fact_status("project", "build_tool").status == "unverified"


def test_contingencies_cascade_and_unverified_status_is_transitive(mem: Memory) -> None:
    health = ("user", "health_condition")
    routine = ("user", "exercise_routine")
    mem.assert_facts(
        [
            FactWrite("user", "health_condition", "lactose intolerance", "e0"),
            FactWrite("user", "exercise_routine", "swimming", "e0", premises=(health,)),
            FactWrite("user", "facility", "Crysthene Pool", "e0", premises=(routine,)),
            FactWrite("user", "locker", "B12", "e0", premises=(("user", "facility"),)),
            FactWrite("user", "exercise_routine", "cycling", "e0", mode="contingent",
                      premises=(health,)),
            FactWrite("user", "facility", "Velthari Studio", "e0", mode="contingent",
                      premises=(routine,)),
        ]
    )  # fmt: skip
    mem.assert_fact("user", "health_condition", "high blood pressure", "e1")
    assert mem.get_fact("user", "exercise_routine").value == "cycling"
    assert mem.get_fact("user", "facility").value == "Velthari Studio"
    locker = mem.fact_status("user", "locker")
    assert locker.status == "unverified" and "user.facility changed" in locker.reasons[0]

    mem.assert_facts([FactWrite("user", "diet", "dairy-free", "e2", premises=(routine,))])
    mem.assert_facts([FactWrite("user", "snack", "oat bar", "e2", premises=(("user", "diet"),))])
    mem.assert_fact("user", "exercise_routine", "rowing", "e3")
    snack = mem.fact_status("user", "snack")
    assert snack.status == "unverified" and "is itself unverified" in snack.reasons[0]


def test_reader_of_a_conditional_fact_is_invalidated_by_its_premise(mem: Memory) -> None:
    setup_build_tool(mem)

    def page(ctx: Context) -> str:
        return f"{ctx.slot('project', 'layout')} ({ctx.status('project', 'layout').status})"

    mem.define("page", "project", 1, page)
    mem.maintain()
    assert mem.node("page", "project").value == "single-app (current)"
    mem.assert_fact("project", "framework", "Veltrion", "e1")
    assert mem.node("page", "project").state == "stale"
    mem.maintain()
    assert mem.node("page", "project").value == "single-app (unverified)"
    mem.assert_fact("project", "layout", "single-app", "e2")  # same value, premise dropped
    assert mem.node("page", "project").state == "stale"


def test_recall_flags_unverified_facts_and_falls_back(mem: Memory) -> None:
    setup_build_tool(mem)
    mem.assert_fact("project", "framework", "Veltrion", "e1")
    res = mem.recall("layout", events=0)
    layout = next(i for i in res.items if i.key == "project/layout")
    assert layout.status == "unverified" and "changed from" in layout.text
    assert res.fallback_used and res.suppressed == 1
    assert all(i.status != "current" for i in res.items if i.key == "project/layout")


def test_contingent_write_validation(mem: Memory) -> None:
    with pytest.raises(ValueError):
        mem.assert_facts([FactWrite("a", "b", 1, "e0", mode="contingent")])
    with pytest.raises(ValueError):
        mem.assert_facts([FactWrite("a", "b", 1, "e0", premises=(("a", "b"),))])


def test_invalidations_are_counted_and_demand_defers_churning_nodes(mem: Memory) -> None:
    mem.assert_facts([FactWrite("x", "v", 0, "e0"), FactWrite("y", "v", 0, "e0")])
    mem.define("n", "churn", 1, lambda c: c.slot("x", "v"))
    mem.define("n", "steady", 1, lambda c: c.slot("y", "v"))
    mem.maintain()
    for i in range(1, 4):
        mem.assert_fact("x", "v", i, f"e{i}")
        mem.maintain()
    assert mem.node("n", "churn").invalidations == 3
    assert mem.node("n", "steady").invalidations == 0
    for _ in range(3):
        mem.read_node("n", "churn")
        mem.read_node("n", "steady")
    mem.assert_fact("x", "v", 9, "e5")
    mem.assert_fact("y", "v", 9, "e5")
    # Same reads and cost: priority falls back to age (churn first); demand prefers steady.
    assert mem.maintain(Budget(1), Policy.PRIORITY).outcomes[0] == "n:churn:computed"
    mem.assert_fact("x", "v", 10, "e6")
    mem.maintain(Budget(1), Policy.DEMAND)
    assert mem.node("n", "steady").state == "valid"
    assert mem.node("n", "churn").state == "stale"


def test_version_3_database_is_migrated_in_place(db_path: str) -> None:
    with Memory.open(db_path) as m:
        m.ingest("s", [Event("e0", "s", "message", {"text": "hello"})])
        m.assert_fact("user", "name", "Ada", "e0")
        m.define("n", "k", 1, lambda c: c.slot("user", "name"))
        m.maintain()
    downgrade_to_v3(db_path)
    with Memory.open(db_path) as m:
        assert m.store.db.execute("SELECT value FROM meta").fetchone()[0] == "4"
        assert m.node("n", "k").state == "valid" and m.node("n", "k").invalidations == 0
        assert m.fact_status("user", "name").status == "current"
