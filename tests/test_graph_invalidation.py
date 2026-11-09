"""Atlas A->B->C flow, diamonds, phantom dependencies, and same-value cutoff."""

from __future__ import annotations

from collections import Counter

from strata import Context, Memory, NodeState


def states(mem: Memory) -> dict[str, str]:
    return {f"{n.kind}/{n.key}": n.state for n in mem.nodes()}


def define_atlas_chain(mem: Memory, calls: Counter[str]) -> None:
    def node_b(ctx: Context) -> dict[str, object]:
        calls["b"] += 1
        return {"launch": ctx.slot("atlas", "launch_date")}

    def node_c(ctx: Context) -> dict[str, str]:
        calls["c"] += 1
        return {"banner": f"Atlas launches {ctx.node('b', 'atlas')['launch']}"}

    mem.define("b", "atlas", 1, node_b)
    mem.define("c", "atlas", 2, node_c)


def test_atlas_chain_direct_stale_transitive_suspect_then_rebuilt(mem: Memory) -> None:
    calls: Counter[str] = Counter()
    define_atlas_chain(mem, calls)
    mem.assert_fact("atlas", "launch_date", "2026-12-10", "e0")
    first = mem.maintain(10)
    assert (first.jobs, first.cost, first.dirty) == (2, 2.0, 0)
    assert mem.node("c", "atlas").value == {"banner": "Atlas launches 2026-12-10"}

    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")
    assert states(mem) == {"b/atlas": NodeState.STALE, "c/atlas": NodeState.SUSPECT}

    report = mem.maintain(10)
    assert report.outcomes == ["b:atlas:computed", "c:atlas:computed"]
    assert mem.node("c", "atlas").value == {"banner": "Atlas launches 2027-01-15"}
    assert calls == Counter(b=2, c=2)
    assert all(s == NodeState.VALID for s in states(mem).values())


def test_transitive_chain_of_three_levels_is_invalidated(mem: Memory) -> None:
    mem.define("l1", "x", 1, lambda c: c.slot("x", "v"))
    mem.define("l2", "x", 2, lambda c: {"v": c.node("l1", "x")})
    mem.define("l3", "x", 3, lambda c: [c.node("l2", "x")])
    mem.assert_fact("x", "v", 1, "e0")
    mem.maintain(10)
    mem.assert_fact("x", "v", 2, "e1")
    assert states(mem) == {
        "l1/x": NodeState.STALE,
        "l2/x": NodeState.SUSPECT,
        "l3/x": NodeState.SUSPECT,
    }
    mem.maintain(10)
    assert mem.node("l3", "x").value == [{"v": 2}]


def test_diamond_recomputes_each_node_once(mem: Memory) -> None:
    calls: Counter[str] = Counter()

    def left(ctx: Context) -> str:
        calls["left"] += 1
        return f"L{ctx.slot('a', 'v')}"

    def right(ctx: Context) -> str:
        calls["right"] += 1
        return f"R{ctx.slot('a', 'v')}"

    def join(ctx: Context) -> str:
        calls["join"] += 1
        return ctx.node("left", "a") + ctx.node("right", "a")

    mem.define("left", "a", 1, left)
    mem.define("right", "a", 1, right)
    mem.define("join", "a", 2, join)
    mem.assert_fact("a", "v", 1, "e0")
    mem.maintain(10)
    calls.clear()

    mem.assert_fact("a", "v", 2, "e1")
    assert states(mem)["join/a"] == NodeState.SUSPECT
    report = mem.maintain(10)
    assert report.jobs == 3
    assert calls == Counter(left=1, right=1, join=1)
    assert mem.node("join", "a").value == "L2R2"
    assert mem.node("join", "a").attempts == 0


def test_phantom_subject_dependency_new_slot_invalidates(mem: Memory) -> None:
    mem.assert_fact("atlas", "owner", "ana", "e0")
    mem.define("page", "atlas", 1, lambda c: c.subject("atlas"))
    mem.maintain(10)
    assert mem.node("page", "atlas").value == {"owner": "ana"}

    mem.assert_fact("atlas", "status", "active", "e1")  # slot did not exist when page was built
    assert states(mem)["page/atlas"] == NodeState.STALE
    mem.maintain(10)
    assert mem.node("page", "atlas").value == {"owner": "ana", "status": "active"}


def test_phantom_dependency_on_slot_that_did_not_exist(mem: Memory) -> None:
    mem.define("n", "x", 1, lambda c: {"seen": c.slot("ghost", "attr", default="missing")})
    mem.maintain(10)
    assert mem.node("n", "x").value == {"seen": "missing"}
    assert mem.dependencies("n", "x")[0].kind == "slot"

    mem.assert_fact("ghost", "attr", "present", "e0")
    assert states(mem)["n/x"] == NodeState.STALE
    mem.maintain(10)
    assert mem.node("n", "x").value == {"seen": "present"}


def test_retraction_invalidates_dependents(mem: Memory) -> None:
    mem.assert_fact("atlas", "owner", "ana", "e0")
    mem.define("n", "atlas", 1, lambda c: {"owner": c.slot("atlas", "owner", "nobody")})
    mem.maintain(10)
    mem.retract_fact("atlas", "owner", "e1")
    assert states(mem)["n/atlas"] == NodeState.STALE
    mem.maintain(10)
    assert mem.node("n", "atlas").value == {"owner": "nobody"}
    assert mem.get_fact("atlas", "owner") is None
    assert [f.op for f in mem.fact_history("atlas", "owner")] == ["assert", "retract"]


def test_reasserting_same_value_does_not_invalidate(mem: Memory) -> None:
    calls: Counter[str] = Counter()

    def node(ctx: Context) -> object:
        calls["n"] += 1
        return ctx.slot("atlas", "owner")

    mem.assert_fact("atlas", "owner", "ana", "e0")
    mem.define("n", "atlas", 1, node)
    mem.maintain(10)
    before = mem.node("n", "atlas")
    mem.assert_fact("atlas", "owner", "ana", "e1")  # same value, new source
    assert states(mem) == {"n/atlas": NodeState.VALID}
    assert mem.maintain(10).jobs == 0
    assert calls["n"] == 1
    assert mem.node("n", "atlas").changed_revision == before.changed_revision
    assert len(mem.fact_history("atlas", "owner")) == 2  # provenance still recorded
    assert mem.get_fact("atlas", "owner").source_event == "e1"


def test_same_value_cutoff_verifies_downstream_for_free(mem: Memory) -> None:
    calls: Counter[str] = Counter()

    def size(ctx: Context) -> dict[str, int]:
        calls["size"] += 1
        return {"len": len(ctx.slot("atlas", "name"))}

    def label(ctx: Context) -> str:
        calls["label"] += 1
        return f"len={ctx.node('size', 'atlas')['len']}"

    mem.assert_fact("atlas", "name", "alpha", "e0")
    mem.define("size", "atlas", 1, size)
    mem.define("label", "atlas", 2, label)
    mem.maintain(10)
    calls.clear()

    mem.assert_fact("atlas", "name", "omega", "e1")  # same length
    report = mem.maintain(10)
    assert (report.jobs, report.cutoffs, report.verified, report.cost) == (1, 1, 1, 1.0)
    assert calls == Counter(size=1)  # label was verified, never recomputed
    assert report.outcomes == ["size:atlas:cutoff", "label:atlas:verified"]
    assert states(mem) == {"size/atlas": NodeState.VALID, "label/atlas": NodeState.VALID}


def test_derivation_code_version_change_invalidates(mem: Memory) -> None:
    mem.assert_fact("a", "v", 1, "e0")
    mem.define("n", "a", 1, lambda c: c.slot("a", "v"), version="1")
    mem.define("m", "a", 2, lambda c: c.node("n", "a"))
    mem.maintain(10)
    mem.define("n", "a", 1, lambda c: c.slot("a", "v") * 10, version="2")
    assert states(mem) == {"n/a": NodeState.STALE, "m/a": NodeState.SUSPECT}
    mem.maintain(10)
    assert mem.node("m", "a").value == 10


def test_rebuild_recomputes_everything(mem: Memory) -> None:
    mem.assert_fact("a", "v", 1, "e0")
    mem.define("n", "a", 1, lambda c: c.slot("a", "v"))
    mem.define("m", "a", 2, lambda c: c.node("n", "a"))
    mem.maintain(10)
    report = mem.rebuild()
    assert report.jobs == 2
    assert report.cutoffs == 2  # nothing actually changed


def test_history_lists_value_changes_and_tracks_the_slot(mem: Memory) -> None:
    def timeline(ctx: Context) -> list[object]:
        return [(c["value"], ctx.event(c["source"])["text"]) for c in ctx.history("atlas", "v")]

    mem.assert_fact("atlas", "v", 1, "e0")
    mem.assert_fact("atlas", "v", 1, "e1")  # same value: not a change
    mem.assert_fact("atlas", "v", 2, "e2")
    mem.define("timeline", "atlas", 1, timeline)
    mem.maintain(10)
    assert mem.node("timeline", "atlas").value == [
        [1, "note e0 about atlas"],
        [2, "note e2 about atlas"],
    ]

    mem.assert_fact("atlas", "v", 2, "e3")  # re-assertion leaves the output, and the node, valid
    assert mem.node("timeline", "atlas").state == NodeState.VALID
    mem.assert_fact("atlas", "v", 3, "e4")
    assert mem.node("timeline", "atlas").state == NodeState.STALE
    mem.maintain(10)
    assert [v for v, _ in mem.node("timeline", "atlas").value] == [1, 2, 3]
