"""Engine performance on a synthetic workload: ingest, maintenance, recall latency, storage.

No model is involved: each event carries one fact, every subject has a built-in ``profile``
node, and maintenance runs the profile derivation. The workload is seeded, so counts are
reproducible; times depend on the machine and on its load, so run it on an idle machine.

For each size the script measures:

* ingest: events stored per second, each with one fact assertion (and FTS indexing);
* initial maintenance: profile builds per second;
* incremental update: 1% of subjects change; jobs and time of ``maintain`` against ``rebuild``;
* recall: p50 and p95 latency over 200 queries;
* storage: database bytes per event.

Usage: PYTHONPATH=src python benchmarks/perf.py [--sizes 1000 10000 100000] [--output perf.json]
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from strata import Event, FactWrite, Memory
from strata import __version__ as strata_version
from strata.profiles import define_profile

ATTRIBUTES = ("owner", "status", "budget", "region")
VALUES = {
    "owner": ["ana", "bo", "cy", "di", "ed"],
    "status": ["planned", "active", "paused", "done"],
    "budget": [100, 250, 500, 1000],
    "region": ["north", "south", "east", "west"],
}
SESSION_EVENTS = 25
QUERIES = 200


def workload(n: int, seed: int) -> list[tuple[str, str, str, Any]]:
    """(subject, attribute, text, value) per event; about 20 events per subject."""
    rng = random.Random(seed)
    subjects = [f"project{i:05d}" for i in range(max(10, n // 20))]
    rows = []
    for _ in range(n):
        subject, attribute = rng.choice(subjects), rng.choice(ATTRIBUTES)
        value = rng.choice(VALUES[attribute])
        rows.append((subject, attribute, f"{subject} {attribute} is now {value}", value))
    return rows


def pctl(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


def run_size(n: int, seed: int) -> dict[str, Any]:
    rows = workload(n, seed)
    rng = random.Random(seed + 1)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "perf.db"
        with Memory.open(str(path)) as m:
            defined: set[str] = set()
            started = time.perf_counter()
            for start in range(0, n, SESSION_EVENTS):
                session = f"s{start // SESSION_EVENTS:06d}"
                batch = rows[start : start + SESSION_EVENTS]
                keys = [f"{session}:{i}" for i in range(len(batch))]
                m.ingest(
                    session,
                    [
                        Event(k, session, "message", {"text": r[2]})
                        for k, r in zip(keys, batch, strict=True)
                    ],
                )
                m.assert_facts(
                    [FactWrite(r[0], r[1], r[3], k) for k, r in zip(keys, batch, strict=True)]
                )
                for subject in {r[0] for r in batch} - defined:
                    define_profile(m, subject)
                    defined.add(subject)
            ingest_s = time.perf_counter() - started

            started = time.perf_counter()
            initial = m.maintain(None)
            initial_s = time.perf_counter() - started

            changed = rng.sample(sorted(defined), max(1, len(defined) // 100))
            session = "update"
            keys = [f"{session}:{i}" for i in range(len(changed))]
            m.ingest(
                session,
                [
                    Event(k, session, "message", {"text": f"{s} status update"})
                    for k, s in zip(keys, changed, strict=True)
                ],
            )
            m.assert_facts(
                [FactWrite(s, "status", "changed", k) for k, s in zip(keys, changed, strict=True)]
            )
            started = time.perf_counter()
            incremental = m.maintain(None)
            incremental_s = time.perf_counter() - started
            started = time.perf_counter()
            rebuild = m.rebuild(None)
            rebuild_s = time.perf_counter() - started

            subjects = sorted(defined)
            latencies = []
            for _ in range(QUERIES):
                query = f"{rng.choice(subjects)} {rng.choice(ATTRIBUTES)}"
                started = time.perf_counter()
                m.recall(query, limit=10)
                latencies.append(1000 * (time.perf_counter() - started))
        db_bytes = sum(p.stat().st_size for p in Path(tmp).glob("perf.db*"))
    return {
        "events": n,
        "subjects": len(defined),
        "ingest_events_per_s": n / ingest_s,
        "initial_jobs": initial.jobs,
        "initial_jobs_per_s": initial.jobs / initial_s if initial_s else None,
        "changed_subjects": len(changed),
        "incremental_jobs": incremental.jobs,
        "incremental_ms": 1000 * incremental_s,
        "rebuild_jobs": rebuild.jobs,
        "rebuild_ms": 1000 * rebuild_s,
        "recall_p50_ms": statistics.median(latencies),
        "recall_p95_ms": pctl(latencies, 0.95),
        "bytes_per_event": db_bytes / n,
    }


def environment() -> dict[str, str]:
    return {
        "strata": strata_version,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "machine": f"{platform.system()} {platform.machine()} {platform.processor()}".strip(),
    }


def render(results: list[dict[str, Any]], env: dict[str, str]) -> str:
    lines = [
        f"Strata {env['strata']}, Python {env['python']}, SQLite {env['sqlite']}, "
        f"{env['machine']}.",
        "",
        "| events | ingest events/s | profile builds/s | 1% update: jobs (ms) | full rebuild: "
        "jobs (ms) | recall p50 / p95 ms | bytes/event |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['events']:,} | {r['ingest_events_per_s']:,.0f} | "
            f"{r['initial_jobs_per_s']:,.0f} | {r['incremental_jobs']} "
            f"({r['incremental_ms']:,.1f}) | {r['rebuild_jobs']:,} ({r['rebuild_ms']:,.1f}) | "
            f"{r['recall_p50_ms']:.1f} / {r['recall_p95_ms']:.1f} | {r['bytes_per_event']:,.0f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 100000])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args(argv)
    env = environment()
    results = []
    for n in args.sizes:
        results.append(run_size(n, args.seed))
        print(f"[perf] {n:,} events done", file=sys.stderr, flush=True)
    print(render(results, env))
    if args.output:
        args.output.write_text(json.dumps({"environment": env, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
