"""Benchmark determinism and invariants (small config for speed)."""

from __future__ import annotations

import json

import pytest

from strata import bench

SMALL = dict(entities=12, teams=3, rounds=10, updates_per_round=4, queries_per_round=8)


def test_same_seed_gives_byte_identical_json() -> None:
    first = bench.render(bench.run(seed=3, **SMALL))
    second = bench.render(bench.run(seed=3, **SMALL))
    assert first == second
    assert json.loads(first)["config"]["seed"] == 3


def test_different_seed_changes_workload() -> None:
    a = bench.run(seed=1, **SMALL)
    b = bench.run(seed=2, **SMALL)
    assert a["strategies"] != b["strategies"]


def test_output_is_stable_json_without_environment_leaks() -> None:
    text = bench.render(bench.run(seed=0, **SMALL))
    assert text.endswith("\n")
    assert "/tmp" not in text and "/var/" not in text
    data = json.loads(text)
    assert json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n" == text
    assert list(data["strategies"]) == sorted(bench.STRATEGIES)


def test_strategies_and_safety_invariants() -> None:
    data = bench.run(seed=0, **SMALL)
    strategies = data["strategies"]
    assert set(strategies) == set(bench.STRATEGIES)
    for name in ("full_rebuild", "fifo", "priority", "lazy_fallback"):
        answers = strategies[name]["answers"]
        assert answers["stale_served"] == 0, name
        assert answers["derived_served_wrong"] == 0, (
            name
        )  # safe strategies never serve wrong values
    full = strategies["full_rebuild"]
    assert full["answers"]["lookup_accuracy"] == 1.0
    assert full["answers"]["aggregate_answered_rate"] == 1.0
    assert strategies["append_only_raw"]["maintenance"]["cost"] == 0
    assert strategies["append_only_raw"]["answers"]["aggregates_answered"] == 0
    # incremental maintenance does strictly less work than rebuilding everything every round
    assert strategies["fifo"]["maintenance"]["cost"] < full["maintenance"]["cost"]
    assert strategies["priority"]["maintenance"]["cost"] < full["maintenance"]["cost"]
    budget = data["config"]["round_budget"] * data["config"]["rounds"]
    assert strategies["fifo"]["maintenance"]["cost"] <= budget
    assert strategies["priority"]["maintenance"]["cost"] <= budget
    # the control proves the staleness metric is not vacuous
    assert strategies["stale_control"]["answers"]["stale_served"] > 0


def test_queries_counted_consistently() -> None:
    data = bench.run(seed=5, **SMALL)
    expected = data["workload"]["queries"]
    for name, result in data["strategies"].items():
        a = result["answers"]
        assert a["lookups"] + a["aggregates"] == expected, name
        assert a["lookups_from_derived"] + a["lookups_from_raw_fallback"] == a["lookups"], name


def test_invalid_config_is_rejected() -> None:
    with pytest.raises(ValueError):
        bench.run(entities=2, teams=5)
    with pytest.raises(ValueError):
        bench.run(raw_top_k=0)
    with pytest.raises(ValueError):
        bench.run(aggregate_share=1.5)


def test_schedule_is_a_pure_function_of_the_seed() -> None:
    cfg = bench.BenchConfig(seed=9, **SMALL)
    assert bench.build_schedule(cfg) == bench.build_schedule(cfg)
