"""Stale-serving safety, provenance, and fallback behaviour."""

from __future__ import annotations

from strata import Context, Event, Memory, NodeState
from strata.profiles import define_profile


def build_atlas(mem: Memory) -> None:
    mem.assert_fact("atlas", "launch_date", "2026-12-10", "e0")
    mem.define("summary", "atlas", 1, lambda c: {"launch": c.slot("atlas", "launch_date")})
    mem.define("banner", "atlas", 2, lambda c: f"Launch {c.node('summary', 'atlas')['launch']}")
    mem.maintain(10)


def kinds(result) -> list[tuple[str, str]]:
    return [(i.kind, i.status) for i in result.items]


def test_current_results_carry_provenance(mem: Memory) -> None:
    build_atlas(mem)
    result = mem.recall("launch")
    assert not result.fallback_used and result.suppressed == 0
    by_kind = {i.kind: i for i in result.items}
    assert set(by_kind) == {"fact", "summary", "banner"}
    assert all(i.status == "current" for i in result.items)
    assert by_kind["fact"].provenance == ("fact:atlas/launch_date@1", "event:e0")
    assert by_kind["banner"].provenance == (
        "event:e0",
        "fact:atlas/launch_date@1",
        "node:summary/atlas",
    )
    assert by_kind["summary"].value == {"launch": "2026-12-10"}


def test_dirty_nodes_are_never_returned_as_current(mem: Memory) -> None:
    mem.ingest("s", [Event("note", "s", "message", {"text": "the launch moved to January"})])
    build_atlas(mem)
    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")  # summary stale, banner suspect
    assert mem.node("banner", "atlas").state == NodeState.SUSPECT
    result = mem.recall("launch")
    assert ("summary", "current") not in kinds(result)
    assert ("banner", "current") not in kinds(result)
    assert ("fact", "current") in kinds(result)
    assert result.suppressed == 2
    assert result.fallback_used
    assert any("withheld" in w for w in result.warnings)
    raw = [i for i in result.items if i.status == "raw"]
    assert raw and all(i.kind == "event" and i.provenance == (f"event:{i.key}",) for i in raw)
    # no item anywhere contains the old, now-wrong date as current
    assert all("2026-12-10" not in i.text for i in result.items if i.status == "current")


def test_include_stale_labels_withheld_items(mem: Memory) -> None:
    build_atlas(mem)
    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")
    result = mem.recall("launch", include_stale=True)
    stale = [i for i in result.items if i.status == "stale"]
    assert {i.kind for i in stale} == {"summary", "banner"}
    assert not [
        i for i in result.items if i.status == "current" and i.kind in ("summary", "banner")
    ]


def test_recall_current_again_after_maintenance(mem: Memory) -> None:
    build_atlas(mem)
    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")
    mem.maintain(10)
    result = mem.recall("launch")
    assert result.suppressed == 0 and not result.fallback_used
    summary = next(i for i in result.items if i.kind == "summary")
    assert summary.value == {"launch": "2027-01-15"}
    assert summary.provenance == ("event:e1", "fact:atlas/launch_date@2")


def test_fallback_when_nothing_current_matches(mem: Memory) -> None:
    result = mem.recall("atlas")  # only raw events exist
    assert result.fallback_used
    assert result.items and all(i.status == "raw" for i in result.items)
    assert mem.recall("atlas", fallback=False).items == ()
    empty = mem.recall("zzzunmatched")
    assert empty.items == () and empty.fallback_used
    assert any("no current memory or raw evidence" in w for w in empty.warnings)


def test_fallback_warning_distinguishes_current_hits_from_no_raw_evidence(mem: Memory) -> None:
    mem.define("summary", "orion", 1, lambda c: "steady")
    build_atlas(mem)  # no raw event mentions a summary or a banner
    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")
    result = mem.recall("summary banner")
    assert result.fallback_used
    assert any("no raw event evidence" in w for w in result.warnings)
    assert not any("no current memory" in w for w in result.warnings)


def test_retracted_fact_is_not_recalled_as_current(mem: Memory) -> None:
    mem.assert_fact("atlas", "codename", "zebra", "e0")
    assert ("fact", "current") in kinds(mem.recall("zebra"))
    mem.retract_fact("atlas", "codename", "e1")
    assert ("fact", "current") not in kinds(mem.recall("zebra", fallback=False))


def test_old_fact_value_disappears_from_index(mem: Memory) -> None:
    mem.assert_fact("atlas", "codename", "zebra", "e0")
    mem.assert_fact("atlas", "codename", "giraffe", "e1")
    assert mem.recall("zebra", fallback=False).items == ()
    assert mem.recall("giraffe", fallback=False).items[0].value == "giraffe"


def test_lazy_budget_repairs_what_the_query_needs(mem: Memory) -> None:
    build_atlas(mem)
    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")
    result = mem.recall("launch", lazy_budget=5)
    assert result.refreshed == 2 and result.suppressed == 0 and not result.fallback_used
    assert next(i for i in result.items if i.kind == "banner").value == "Launch 2027-01-15"


def test_insufficient_lazy_budget_still_withholds(mem: Memory) -> None:
    build_atlas(mem)
    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")
    result = mem.recall("launch", lazy_budget=1)  # enough for summary, not banner
    assert result.refreshed == 1
    assert ("banner", "current") not in kinds(result)
    assert ("summary", "current") in kinds(result)
    assert result.suppressed == 1 and result.fallback_used


def test_read_node_is_safe_and_can_refresh_on_demand(mem: Memory) -> None:
    build_atlas(mem)
    assert mem.read_node("banner", "atlas").value == "Launch 2026-12-10"
    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")
    unavailable = mem.read_node("banner", "atlas")
    assert unavailable.status == "unavailable" and unavailable.value is None
    assert unavailable.state == NodeState.SUSPECT
    refreshed = mem.read_node("banner", "atlas", refresh_budget=10)
    assert refreshed.status == "current" and refreshed.refreshed
    assert refreshed.value == "Launch 2027-01-15"
    assert mem.node("banner", "atlas").access_count == 3


def test_failed_node_is_never_served(mem: Memory) -> None:
    def explode(ctx: Context) -> int:
        raise RuntimeError("no")

    mem.assert_fact("a", "v", 1, "e0")
    mem.define("n", "a", 1, lambda c: c.slot("a", "v"))
    mem.maintain(10)
    mem.define("n", "a", 1, explode, version="2")
    for _ in range(3):
        mem.maintain(10)
    assert mem.node("n", "a").state == NodeState.FAILED
    assert mem.read_node("n", "a").status == "unavailable"
    assert not [i for i in mem.recall("a", fallback=False).items if i.kind == "n"]


def test_query_text_is_sanitised(mem: Memory) -> None:
    build_atlas(mem)
    for query in ['atlas "launch', "launch AND OR NOT (", "***", "  ", "a:b NEAR/2", "日本語"]:
        mem.recall(query)  # must not raise an FTS syntax error
    assert mem.recall("   ").items == ()


def test_search_events_is_plain_raw_search(mem: Memory) -> None:
    hits = mem.search_events("e3", limit=3)
    assert hits and all(h.status == "raw" for h in hits)
    assert hits[0].key == "e3" and hits[0].value == {"text": "note e3 about atlas"}


def test_events_adds_fact_sources_then_search_hits(mem: Memory) -> None:
    mem.assert_fact("atlas", "codename", "zebra", "e3")
    result = mem.recall("zebra atlas", events=3)
    assert not result.fallback_used
    assert [(i.kind, i.status) for i in result.items][:1] == [("fact", "current")]
    raw = [i for i in result.items if i.status == "raw"]
    assert len(raw) == 3 and len({i.key for i in raw}) == 3
    assert raw[0].key == "e3"  # the fact's source event comes first
    assert any("unverified evidence" in w for w in result.warnings)


def test_events_default_keeps_current_memory_only(mem: Memory) -> None:
    mem.assert_fact("atlas", "codename", "zebra", "e3")
    assert kinds(mem.recall("zebra atlas")) == [("fact", "current")]


def test_events_never_relabel_stale_nodes(mem: Memory) -> None:
    build_atlas(mem)
    mem.assert_fact("atlas", "launch_date", "2027-01-15", "e1")
    result = mem.recall("launch", events=4)
    assert result.suppressed == 2 and result.fallback_used
    assert ("summary", "current") not in kinds(result)
    assert all("2026-12-10" not in i.text for i in result.items if i.status == "current")


def test_unbuilt_node_is_withheld_and_can_be_built_lazily(mem: Memory) -> None:
    mem.assert_fact("atlas", "launch_date", "2026-12-10", "e0")
    mem.define("summary", "atlas", 1, lambda c: {"launch": c.slot("atlas", "launch_date")})
    result = mem.recall("summary")
    assert result.suppressed == 1 and result.fallback_used
    assert not [i for i in mem.recall("summary", include_stale=True).items if i.kind == "summary"]
    lazy = mem.recall("summary", lazy_budget=1)
    assert lazy.refreshed == 1 and lazy.suppressed == 0
    assert next(i for i in lazy.items if i.kind == "summary").value == {"launch": "2026-12-10"}


def test_lazy_budget_builds_a_chain_of_never_built_nodes(mem: Memory) -> None:
    mem.assert_fact("atlas", "launch_date", "2026-12-10", "e0")
    mem.define("summary", "atlas", 1, lambda c: {"launch": c.slot("atlas", "launch_date")})
    mem.define("banner", "atlas", 2, lambda c: f"Launch {c.node('summary', 'atlas')['launch']}")
    # The banner's dependency on the summary is only discovered when it first runs.
    lazy = mem.recall("banner", lazy_budget=3)
    assert lazy.refreshed == 2 and lazy.suppressed == 0
    assert next(i for i in lazy.items if i.kind == "banner").value == "Launch 2026-12-10"
    assert mem.read_node("banner", "atlas").status == "current"


def test_search_text_controls_how_a_built_node_is_indexed(mem: Memory) -> None:
    mem.assert_fact("atlas", "launch_date", "2026-12-10", "e0")
    mem.define(
        "summary",
        "atlas",
        1,
        lambda c: c.slot("atlas", "launch_date"),
        search_text=lambda v: "milestone",
    )
    mem.maintain(10)
    assert ("summary", "current") in kinds(mem.recall("milestone", fallback=False))
    assert ("summary", "current") not in kinds(mem.recall("2026", fallback=False))


def test_profile_matches_attribute_names_not_values(mem: Memory) -> None:
    mem.assert_fact("atlas", "codename", "zebra", "e0")
    define_profile(mem, "atlas")
    mem.maintain(10)
    assert kinds(mem.recall("zebra", fallback=False)) == [("fact", "current")]
    assert ("profile", "current") in kinds(mem.recall("codename", fallback=False))


def test_explain_reports_graph(mem: Memory) -> None:
    build_atlas(mem)
    info = mem.explain("summary", "atlas")
    assert info["dependents"] == ["banner/atlas"]
    assert info["dependencies"][0]["kind"] == "slot"
    assert info["provenance"] == ["event:e0", "fact:atlas/launch_date@1"]
    assert mem.dependents("summary", "atlas") == [("banner", "atlas")]


def test_keywords_make_an_unbuilt_node_findable_and_can_change(mem: Memory) -> None:
    mem.assert_fact("user", "books", ["Dune"], "e0")
    mem.define("digest", "reading", 1, lambda c: {"n": len(c.slot("user", "books"))})
    assert mem.recall("books", lazy_budget=2).refreshed == 0  # only "digest reading" is indexed
    mem.define("digest", "reading", 1, lambda c: {"n": len(c.slot("user", "books"))},
               keywords="books novels")  # fmt: skip
    assert mem.node("digest", "reading").state == NodeState.PENDING  # no invalidation
    lazy = mem.recall("novels", lazy_budget=2)
    assert lazy.refreshed == 1 and lazy.items[0].value == {"n": 1}
    mem.define("digest", "reading", 1, lambda c: {"n": len(c.slot("user", "books"))},
               keywords="library")  # fmt: skip
    assert [i.kind for i in mem.recall("library").items] == ["digest"]
    assert not [i for i in mem.recall("novels").items if i.kind == "digest"]
    assert mem.node("digest", "reading").state == NodeState.VALID
