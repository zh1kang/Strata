"""Command-line interface. Every command prints one JSON document to stdout."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from . import bench
from .errors import StrataError
from .memory import Memory
from .profiles import define_profile, rebind_profiles
from .types import Budget, Event, FactWrite, Policy


def _json_value(text: str) -> Any:
    """Parse as JSON, falling back to a plain string (so ``--value atlas`` works)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="strata", description="Strata memory engine")
    p.add_argument("--db", default=os.environ.get("STRATA_DB", "strata.db"), help="database path")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database if needed")
    sub.add_parser("status", help="counts of events, facts, and nodes by state")

    s = sub.add_parser("ingest", help="append events (idempotent by key)")
    s.add_argument("--session", help="session id (required with --key)")
    s.add_argument("--key", help="event key")
    s.add_argument("--kind", default="message")
    group = s.add_mutually_exclusive_group()
    group.add_argument("--text", help="payload text")
    group.add_argument("--payload", help="payload as a JSON object")
    group.add_argument("--file", help="JSONL file of {key, session_id, kind, payload}")

    s = sub.add_parser("assert", help="assert a fact")
    s.add_argument("subject")
    s.add_argument("attribute")
    s.add_argument("value", help="JSON, or a bare string")
    s.add_argument("--source", required=True, help="source event key")
    s.add_argument("--expect-version", type=int, help="compare-and-set (0 = must not exist)")

    s = sub.add_parser("retract", help="retract a fact")
    s.add_argument("subject")
    s.add_argument("attribute")
    s.add_argument("--source", required=True)
    s.add_argument("--expect-version", type=int)

    s = sub.add_parser("facts", help="live facts of a subject")
    s.add_argument("subject")
    s = sub.add_parser("history", help="all versions of a slot")
    s.add_argument("subject")
    s.add_argument("attribute")

    s = sub.add_parser("define-profile", help="define a profile node (all facts of a subject)")
    s.add_argument("subject")
    s.add_argument("--cost", type=float, default=1.0)

    s = sub.add_parser("maintain", help="repair dirty nodes within a budget")
    s.add_argument("--budget", type=float, help="cost budget (default: unlimited)")
    s.add_argument("--policy", choices=[p.value for p in Policy], default=Policy.PRIORITY.value)
    s = sub.add_parser("rebuild", help="mark every node stale and recompute")
    s.add_argument("--budget", type=float)
    s.add_argument("--policy", choices=[p.value for p in Policy], default=Policy.FIFO.value)
    sub.add_parser("retry-failed", help="requeue failed nodes")

    s = sub.add_parser("recall", help="search current memory with safe fallback")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--include-stale", action="store_true")
    s.add_argument("--no-fallback", action="store_true")
    s.add_argument("--lazy-budget", type=float)
    s.add_argument("--events", type=int, default=0, help="raw events to add next to facts")

    s = sub.add_parser("nodes", help="list derived nodes")
    s.add_argument("--state")
    s = sub.add_parser("explain", help="dependencies and provenance of a node")
    s.add_argument("kind")
    s.add_argument("key")

    s = sub.add_parser("bench", help="run the deterministic benchmark")
    bench.add_arguments(s)
    return p


def _events_from_file(path: str) -> list[Event]:
    events = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                events.append(
                    Event(raw["key"], raw["session_id"], raw.get("kind", "message"), raw["payload"])
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{number}: invalid event line ({exc})") from exc
    return events


def _ingest(mem: Memory, args: argparse.Namespace) -> Any:
    if args.file:
        events = _events_from_file(args.file)
        by_session: dict[str, list[Event]] = {}
        for e in events:
            by_session.setdefault(e.session_id, []).append(e)
        inserted = duplicates = 0
        for session, batch in by_session.items():
            result = mem.ingest(session, batch)
            inserted += result.inserted
            duplicates += result.duplicates
        return {"inserted": inserted, "duplicates": duplicates}
    if not (args.key and args.session):
        raise ValueError("--key and --session are required (or use --file)")
    if args.text is not None:
        payload: Any = {"text": args.text}
    elif args.payload is not None:
        payload = json.loads(args.payload)
    else:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    return asdict(mem.ingest(args.session, [Event(args.key, args.session, args.kind, payload)]))


def _dispatch(mem: Memory, args: argparse.Namespace) -> Any:
    cmd = args.command
    if cmd == "init":
        return {"ok": True, "status": mem.status()}
    if cmd == "status":
        return mem.status()
    if cmd == "ingest":
        return _ingest(mem, args)
    if cmd == "assert":
        write = FactWrite(
            args.subject, args.attribute, _json_value(args.value), args.source, args.expect_version
        )
        return {"version_id": mem.assert_facts([write])[0]}
    if cmd == "retract":
        return {
            "version_id": mem.retract_fact(
                args.subject, args.attribute, args.source, args.expect_version
            )
        }
    if cmd == "facts":
        return [asdict(f) for f in mem.facts(args.subject)]
    if cmd == "history":
        return [asdict(f) for f in mem.fact_history(args.subject, args.attribute)]
    if cmd == "define-profile":
        define_profile(mem, args.subject, args.cost)
        return asdict(mem.node("profile", args.subject))
    if cmd == "maintain":
        rebind_profiles(mem)
        budget = None if args.budget is None else Budget(args.budget)
        return mem.maintain(budget, args.policy).as_dict()
    if cmd == "rebuild":
        rebind_profiles(mem)
        budget = None if args.budget is None else Budget(args.budget)
        return mem.rebuild(budget, args.policy).as_dict()
    if cmd == "retry-failed":
        return {"requeued": mem.retry_failed()}
    if cmd == "recall":
        rebind_profiles(mem)
        result = mem.recall(
            args.query,
            limit=args.limit,
            include_stale=args.include_stale,
            fallback=not args.no_fallback,
            lazy_budget=args.lazy_budget,
            events=args.events,
        )
        return asdict(result)
    if cmd == "nodes":
        return [asdict(n) for n in mem.nodes(args.state)]
    if cmd == "explain":
        return mem.explain(args.kind, args.key)
    raise ValueError(f"unhandled command {cmd!r}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "bench":
            sys.stdout.write(bench.run_from_args(args))
            return 0
        with Memory.open(args.db) as mem:
            result = _dispatch(mem, args)
        sys.stdout.write(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
        return 0
    except (StrataError, ValueError, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
