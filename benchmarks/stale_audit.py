"""Staleness audit for MemDerive: how wrong are the out-of-date pages a system serves?

A system that serves stale pages (``untracked``, ``stale_priority_15``) shows a page that may
predate the latest facts. ``eager`` keeps every page current, so for the same subject at the
same checkpoint its page is the reference. Equal text means the served page is current; for
pages that differ, a judge model counts the statements in the served page that the current page
contradicts. Pages are read from the rendered contexts written by ``memderive.py simulate``.

Usage: python stale_audit.py [--worlds ...] [--system untracked] [--limit 250] [--judge ...]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
from typing import Any

import memderive as md
from lme_models import ModelRequest, ResponseStore, make_backend
from longmemeval import wilson

PAGE = re.compile(r"^\[memory page: (?P<key>[^\]]+)\] \(sources: [^)]*\)\n", re.M)
BLOCK_START = re.compile(r"^\[(?:log\]|fact|memory page: )", re.M)

PROMPT = """You compare two versions of a memory page about the same subject.
CURRENT was rebuilt from every fact known now. SERVED was built earlier and may be out of date.

Count the statements in SERVED that are wrong now: a value, status, total, date, eligibility or
rule outcome that CURRENT contradicts or reports differently. Do not count wording, ordering,
formatting or citations. Count separately the facts CURRENT states that SERVED lacks entirely.
0 is a valid answer for either count.

Reply with only a JSON object:
{{"contradicted": <int>, "missing": <int>,
 "examples": ["<served statement> -> <current statement>", ...]}}
Give at most 3 examples of contradicted statements.

CURRENT:
{current}

SERVED:
{served}
"""


def pages(rendered: str) -> dict[str, str]:
    """Page key -> page text for every memory page block in a rendered context."""
    out = {}
    for m in PAGE.finditer(rendered):
        nxt = BLOCK_START.search(rendered, m.end())
        out[m.group("key")] = rendered[m.end() : nxt.start() if nxt else len(rendered)].strip()
    return out


def checkpoint_pages(world: str, system: str) -> dict[str, dict[str, str]]:
    """Checkpoint -> page key -> text, merged over every question's context."""
    ctx = md.read_json(md.OUT / "contexts" / f"{world}__{system}.json")["contexts"]
    out: dict[str, dict[str, str]] = {}
    for cid, by_q in ctx.items():
        merged = out.setdefault(cid, {})
        for c in by_q.values():
            merged.update(pages(c["rendered"]))
    return out


def parse(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        row = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    counts = [row.get("contradicted"), row.get("missing")]
    if not all(type(n) is int and n >= 0 for n in counts):
        return None
    return {"contradicted": counts[0], "missing": counts[1], "examples": row.get("examples", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--worlds", nargs="+", default=list(md.HELDOUT_WORLDS))
    ap.add_argument("--system", default="untracked")
    ap.add_argument("--limit", type=int, default=250, help="pages sent to the judge")
    ap.add_argument("--judge", default="cursor:grok-4.7-low")
    ap.add_argument("--max-calls", type=int, default=12)
    ap.add_argument("--tag", default="heldout")
    args = ap.parse_args()

    pairs, identical, compared = [], 0, 0
    for w in args.worlds:
        ref, served = checkpoint_pages(w, "eager"), checkpoint_pages(w, args.system)
        for cid, by_key in served.items():
            for key, text in by_key.items():
                current = ref.get(cid, {}).get(key)
                if current is None:
                    continue
                compared += 1
                if text == current:
                    identical += 1
                else:
                    pairs.append(
                        {
                            "world": w,
                            "checkpoint": cid,
                            "page": key,
                            "current": current,
                            "served": text,
                        }
                    )
    sample = random.Random(0).sample(pairs, min(args.limit, len(pairs)))

    judge = make_backend(args.judge, ResponseStore(md.OUT / "responses.sqlite"))

    def run(p: dict[str, Any]) -> None:
        request = ModelRequest(
            PROMPT.format(current=p["current"], served=p["served"]), max_tokens=800
        )
        p["verdict"] = parse(judge.call(request).text)

    _, errors = md.run_pool("audit", args.max_calls, sample, run)
    judged = [p for p in sample if p.get("verdict")]
    wrong = [p for p in judged if p["verdict"]["contradicted"] > 0]
    rep = {
        "system": args.system,
        "worlds": args.worlds,
        "judge": args.judge,
        "pages_compared": compared,
        "pages_identical_to_current": identical,
        "pages_differing": len(pairs),
        "sampled": len(sample),
        "judged": len(judged),
        "with_wrong_statement": len(wrong),
        "wrong_statements_mean": statistics.fmean(p["verdict"]["contradicted"] for p in judged)
        if judged
        else None,
        "with_missing_only": sum(
            p["verdict"]["contradicted"] == 0 and p["verdict"]["missing"] > 0 for p in judged
        ),
        "verdicts": [
            {k: p[k] for k in ("world", "checkpoint", "page")} | (p.get("verdict") or {})
            for p in sample
        ],
        "errors": errors[:20],
    }
    md.RESULTS.mkdir(exist_ok=True)
    share_diff = len(pairs) / compared if compared else 0.0
    share_wrong = len(wrong) / len(judged) if judged else 0.0
    lo, hi = wilson(len(wrong), len(judged)) if judged else (0.0, 0.0)
    # Differing pages are a full census, so only the judged share carries sampling error.
    rep["differing_with_wrong_statement"] = {"rate": share_wrong, "ci95": [lo, hi]}
    rep["served_pages_with_wrong_statement"] = {
        "rate": share_diff * share_wrong,
        "ci95": [share_diff * lo, share_diff * hi],
    }
    md.write_json(md.RESULTS / f"stale_audit_{args.system}_{args.tag}.json", rep)
    print(
        f"{args.system}: {compared} served pages compared with eager's; {len(pairs)} "
        f"({share_diff:.1%}) differ. Of {len(judged)} differing pages judged, {len(wrong)} "
        f"({share_wrong:.1%}, 95% CI {lo:.1%}-{hi:.1%}) contain at least one wrong statement, "
        f"so about {share_diff * share_wrong:.1%} of all served pages do."
    )
    if errors:
        print(f"{len(errors)} errors, e.g. {errors[0]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
