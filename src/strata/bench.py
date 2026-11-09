"""Deterministic seeded benchmark of maintenance strategies on a synthetic workload.

All strategies replay the *same* schedule (generated once from the seed) against
their own fresh database, so differences come from the strategy alone. Costs are
abstract derivation cost units, never wall-clock time, and the JSON output
contains no timestamps, paths, or timings, so identical arguments give
byte-identical output.

This measures maintenance cost and answer correctness on a synthetic workload
that this module generates. It says nothing about real agent workloads.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from . import __version__
from .context import Context
from .memory import Memory
from .types import Budget, Event, Policy

BENCHMARK_ID = "strata-synthetic-v1"
ATTRIBUTES = ("owner", "status", "priority")
VALUES = {
    "owner": ("ana", "bo", "cy", "di", "ed", "flo"),
    "status": ("planned", "active", "launched", "paused"),
    "priority": ("low", "medium", "high"),
}
STRATEGIES = (
    "full_rebuild",
    "append_only_raw",
    "fifo",
    "priority",
    "lazy_fallback",
    "stale_control",
)


@dataclass(frozen=True)
class BenchConfig:
    seed: int = 0
    entities: int = 40
    teams: int = 5
    rounds: int = 40
    updates_per_round: int = 8
    queries_per_round: int = 12
    aggregate_share: float = 0.25
    round_budget: float = 6.0  # background maintenance per round (fifo, priority, control)
    lazy_query_budget: float = 8.0  # on-demand repair allowed per query (lazy_fallback)
    profile_cost: float = 1.0
    team_cost: float = 3.0
    raw_top_k: int = 3

    def validate(self) -> None:
        if self.entities < 1 or self.teams < 1 or self.teams > self.entities:
            raise ValueError("need 1 <= teams <= entities")
        if min(self.rounds, self.updates_per_round, self.queries_per_round) < 0:
            raise ValueError("rounds, updates, and queries must be non-negative")
        if not 0.0 <= self.aggregate_share <= 1.0:
            raise ValueError("aggregate_share must be within [0, 1]")
        if self.raw_top_k < 1:
            raise ValueError("raw_top_k must be at least 1")


@dataclass(frozen=True)
class Update:
    key: str
    entity: str
    attribute: str
    value: str
    text: str


@dataclass(frozen=True)
class Query:
    kind: str  # "lookup" | "aggregate"
    entity: str  # entity for lookups, team name for aggregates
    attribute: str
    truth: Any


@dataclass(frozen=True)
class Round:
    updates: tuple[Update, ...]
    queries: tuple[Query, ...]


@dataclass(frozen=True)
class Schedule:
    initial: tuple[Update, ...]
    rounds: tuple[Round, ...]
    order: dict[str, int]  # event key -> ingestion position


def _entity(i: int) -> str:
    return f"proj{i:02d}"


def _team(i: int, teams: int) -> str:
    return f"team{i % teams}"


def build_schedule(cfg: BenchConfig) -> Schedule:
    cfg.validate()
    rng = random.Random(cfg.seed)
    state: dict[tuple[str, str], str] = {}
    initial: list[Update] = []
    order: dict[str, int] = {}
    for i in range(cfg.entities):
        for attr in ATTRIBUTES:
            value = rng.choice(VALUES[attr])
            state[(_entity(i), attr)] = value
            key = f"init-{_entity(i)}-{attr}"
            initial.append(Update(key, _entity(i), attr, value, f"{_entity(i)} {attr} is {value}"))
            order[key] = len(order)
    weights = [1.0 / (rank + 1) for rank in range(cfg.entities)]  # skewed read popularity
    rounds: list[Round] = []
    for r in range(cfg.rounds):
        updates = []
        for k in range(cfg.updates_per_round):
            i = rng.randrange(cfg.entities)
            attr = rng.choice(ATTRIBUTES)
            value = rng.choice(VALUES[attr])
            state[(_entity(i), attr)] = value
            key = f"u{r:03d}-{k:02d}"
            text = f"{_entity(i)} {attr} changed to {value}"
            updates.append(Update(key, _entity(i), attr, value, text))
            order[key] = len(order)
        queries = []
        for _ in range(cfg.queries_per_round):
            if rng.random() < cfg.aggregate_share:
                t = rng.randrange(cfg.teams)
                name = f"team{t}"
                launched = sum(
                    1
                    for i in range(cfg.entities)
                    if _team(i, cfg.teams) == name and state[(_entity(i), "status")] == "launched"
                )
                queries.append(Query("aggregate", name, "launched", launched))
            else:
                i = rng.choices(range(cfg.entities), weights)[0]
                attr = rng.choice(ATTRIBUTES)
                queries.append(Query("lookup", _entity(i), attr, state[(_entity(i), attr)]))
        rounds.append(Round(tuple(updates), tuple(queries)))
    return Schedule(tuple(initial), tuple(rounds), order)


def _event(u: Update) -> Event:
    payload = {"entity": u.entity, "attribute": u.attribute, "value": u.value, "text": u.text}
    return Event(u.key, "bench", "message", payload)


def _define_nodes(mem: Memory, cfg: BenchConfig) -> None:
    def profile(ctx: Context) -> dict[str, Any]:
        return ctx.subject(ctx.key)

    def team_summary_for(team: str, members: Sequence[str]) -> Any:
        def team_summary(ctx: Context) -> dict[str, Any]:
            launched = sum(1 for m in members if ctx.node("profile", m).get("status") == "launched")
            return {"team": team, "launched": launched}

        return team_summary

    for i in range(cfg.entities):
        mem.define("profile", _entity(i), 1, profile, cfg.profile_cost)
    for t in range(cfg.teams):
        name = f"team{t}"
        members = [_entity(i) for i in range(cfg.entities) if _team(i, cfg.teams) == name]
        mem.define("team", name, 2, team_summary_for(name, members), cfg.team_cost)


def _raw_lookup(mem: Memory, order: dict[str, int], cfg: BenchConfig, q: Query) -> str | None:
    """Most recent top-k BM25 hit that describes the asked entity and attribute."""
    hits = mem.search_events(f"{q.entity} {q.attribute}", limit=cfg.raw_top_k)
    matching = [
        h for h in hits if h.value["entity"] == q.entity and h.value["attribute"] == q.attribute
    ]
    if not matching:
        return None
    return str(max(matching, key=lambda h: order[h.key]).value["value"])


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 6)


def run_strategy(name: str, cfg: BenchConfig, schedule: Schedule) -> dict[str, Any]:
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}")
    derived = name != "append_only_raw"
    counts = {
        "lookups": 0,
        "lookups_correct": 0,
        "lookups_from_derived": 0,
        "lookups_from_raw_fallback": 0,
        "aggregates": 0,
        "aggregates_answered": 0,
        "aggregates_correct": 0,
        "derived_served_wrong": 0,
        "stale_served": 0,
    }
    dirty_samples = 0
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory.open(str(Path(tmp) / "bench.db"))
        try:
            mem.ingest("bench", [_event(u) for u in schedule.initial])
            initial_cost = 0.0
            if derived:
                for u in schedule.initial:
                    mem.assert_fact(u.entity, u.attribute, u.value, u.key)
                _define_nodes(mem, cfg)
                initial_cost = mem.maintain(None, Policy.FIFO).cost
            base = replace(mem.engine.totals)  # exclude the initial build from round totals
            for rnd in schedule.rounds:
                for u in rnd.updates:
                    mem.ingest("bench", [_event(u)])
                    if derived:
                        mem.assert_fact(u.entity, u.attribute, u.value, u.key)
                if name == "full_rebuild":
                    mem.rebuild(None)
                elif name in ("fifo", "stale_control"):
                    mem.maintain(Budget(cfg.round_budget), Policy.FIFO)
                elif name == "priority":
                    mem.maintain(Budget(cfg.round_budget), Policy.PRIORITY)
                if derived:
                    dirty_samples += mem.engine.dirty_count()
                for q in rnd.queries:
                    _answer(mem, name, cfg, schedule, q, counts)
            totals = mem.engine.totals
            maintenance = {
                "cost": round(totals.cost - base.cost, 6),
                "jobs": totals.jobs - base.jobs,
                "wasted_cost": round(totals.wasted_cost - base.wasted_cost, 6),
                "verified": totals.verified - base.verified,
                "cutoffs": totals.cutoffs - base.cutoffs,
                "write_amplification": totals.write_amplification - base.write_amplification,
                "initial_build_cost": round(initial_cost, 6),
                "final_dirty_nodes": mem.engine.dirty_count() if derived else 0,
                "mean_dirty_nodes_per_round": _mean(dirty_samples, cfg.rounds) if derived else 0.0,
            }
        finally:
            mem.close()
    total_queries = counts["lookups"] + counts["aggregates"]
    correct = counts["lookups_correct"] + counts["aggregates_correct"]
    return {
        "maintenance": maintenance,
        "answers": {
            **counts,
            "lookup_accuracy": _rate(counts["lookups_correct"], counts["lookups"]),
            "lookup_fallback_rate": _rate(counts["lookups_from_raw_fallback"], counts["lookups"]),
            "aggregate_answered_rate": _rate(counts["aggregates_answered"], counts["aggregates"]),
            "aggregate_accuracy_of_answered": _rate(
                counts["aggregates_correct"], counts["aggregates_answered"]
            ),
            "overall_correct_rate": _rate(correct, total_queries),
        },
    }


def _mean(total: int, n: int) -> float:
    return 0.0 if n == 0 else round(total / n, 6)


def _answer(
    mem: Memory,
    name: str,
    cfg: BenchConfig,
    schedule: Schedule,
    q: Query,
    counts: dict[str, int],
) -> None:
    refresh = cfg.lazy_query_budget if name == "lazy_fallback" else None
    if q.kind == "lookup":
        counts["lookups"] += 1
        answer: Any = None
        if name == "append_only_raw":
            answer = _raw_lookup(mem, schedule.order, cfg, q)
            counts["lookups_from_raw_fallback"] += 1
        elif name == "stale_control":
            node = mem.node("profile", q.entity)
            if node.value is not None:
                answer = node.value.get(q.attribute)
                counts["lookups_from_derived"] += 1
                counts["stale_served"] += int(node.state != "valid")
                counts["derived_served_wrong"] += int(answer != q.truth)
            else:
                answer = _raw_lookup(mem, schedule.order, cfg, q)
                counts["lookups_from_raw_fallback"] += 1
        else:
            read = mem.read_node("profile", q.entity, refresh_budget=refresh)
            if read.status == "current":
                answer = read.value.get(q.attribute)
                counts["lookups_from_derived"] += 1
                counts["derived_served_wrong"] += int(answer != q.truth)
            else:
                answer = _raw_lookup(mem, schedule.order, cfg, q)
                counts["lookups_from_raw_fallback"] += 1
        counts["lookups_correct"] += int(answer == q.truth)
        return
    counts["aggregates"] += 1
    if name == "append_only_raw":
        return  # raw search cannot aggregate; recorded as unanswered
    if name == "stale_control":
        node = mem.node("team", q.entity)
        if node.value is None:
            return
        answer = node.value["launched"]
        counts["stale_served"] += int(node.state != "valid")
        counts["derived_served_wrong"] += int(answer != q.truth)
    else:
        read = mem.read_node("team", q.entity, refresh_budget=refresh)
        if read.status != "current":
            return
        answer = read.value["launched"]
        counts["derived_served_wrong"] += int(answer != q.truth)
    counts["aggregates_answered"] += 1
    counts["aggregates_correct"] += int(answer == q.truth)


def run(config: BenchConfig | None = None, **overrides: Any) -> dict[str, Any]:
    cfg = config or BenchConfig(**overrides)
    schedule = build_schedule(cfg)
    return {
        "benchmark": BENCHMARK_ID,
        "strata_version": __version__,
        "cost_unit": "abstract derivation cost units, not wall-clock time",
        "config": asdict(cfg),
        "workload": {
            "initial_events": len(schedule.initial),
            "update_events": sum(len(r.updates) for r in schedule.rounds),
            "queries": sum(len(r.queries) for r in schedule.rounds),
        },
        "strategies": {name: run_strategy(name, cfg, schedule) for name in STRATEGIES},
    }


def render(result: dict[str, Any]) -> str:
    return json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    defaults = BenchConfig()
    parser.add_argument("--seed", type=int, default=defaults.seed)
    parser.add_argument("--entities", type=int, default=defaults.entities)
    parser.add_argument("--teams", type=int, default=defaults.teams)
    parser.add_argument("--rounds", type=int, default=defaults.rounds)
    parser.add_argument("--updates-per-round", type=int, default=defaults.updates_per_round)
    parser.add_argument("--queries-per-round", type=int, default=defaults.queries_per_round)
    parser.add_argument("--round-budget", type=float, default=defaults.round_budget)
    parser.add_argument("--lazy-query-budget", type=float, default=defaults.lazy_query_budget)
    parser.add_argument("--output", help="also write the JSON to this file")


def config_from_args(args: argparse.Namespace) -> BenchConfig:
    return BenchConfig(
        seed=args.seed,
        entities=args.entities,
        teams=args.teams,
        rounds=args.rounds,
        updates_per_round=args.updates_per_round,
        queries_per_round=args.queries_per_round,
        round_budget=args.round_budget,
        lazy_query_budget=args.lazy_query_budget,
    )


def run_from_args(args: argparse.Namespace) -> str:
    out = render(run(config_from_args(args)))
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic Strata maintenance benchmark")
    add_arguments(parser)
    sys.stdout.write(run_from_args(parser.parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
