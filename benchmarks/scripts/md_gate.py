"""Fail (exit 1) if a MemDerive stage left failures that are never retried."""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "out" / "memderive"
stage, worlds = sys.argv[1], sys.argv[2:]
bad = []
if stage == "extract":
    for w in worlds:
        log = json.loads((OUT / "factlogs" / f"{w}.json").read_text())
        if log["failed"]:
            bad.append(f"{w}: failed extraction sessions {log['failed']}")
elif stage == "simulate":
    for w in worlds:
        files = sorted((OUT / "contexts").glob(f"{w}__*.json"))
        if not files:
            bad.append(f"{w}: no contexts")
        for f in files:
            d = json.loads(f.read_text())
            if d["maintenance"]["failures"]:
                bad.append(f"{f.name}: {d['maintenance']['failures']} page failures")
            failed = d["status"].get("nodes", {}).get("failed", 0)
            if failed:
                bad.append(f"{f.name}: {failed} failed nodes")
elif stage == "parsed":  # parsed <answerer-slug> <world> <system>...
    slug, world, systems = worlds[0], worlds[1], worlds[2:]
    for system in systems:
        rows = json.loads((OUT / "answers" / f"{world}__{system}__{slug}.json").read_text())
        share = sum(r["parsed"] for r in rows.values()) / max(1, len(rows))
        if share < 0.95:
            bad.append(f"{world}/{system}: only {share:.1%} of answers parsed")
print(f"[gate {stage}] {'OK' if not bad else 'FAIL'}", *bad, sep="\n  ")
sys.exit(1 if bad else 0)
