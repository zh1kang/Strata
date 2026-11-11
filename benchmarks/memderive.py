"""Maintenance benchmark on MemDerive (DeriveMem worlds): does incremental, dependency-tracked
maintenance give the quality of full consolidation for less compute, and does a budgeted,
prioritised schedule degrade safely?

Each world is an activity log of ~450 dated events in ~77 sessions whose questions have answers
derived from facts plus versioned rules. Gold is recompiled by the dataset's own oracle at five
checkpoints (complete session prefixes at 20/40/60/80/100%), so answers change as the stream
advances and scoring is deterministic (no LLM judge).

Pipeline per world: every Strata system shares one LLM fact extraction (per group of sessions,
cached). Systems with pages define an LLM-written page per subject (level 1); a page reads its
subject's dated facts, the subjects its facts name, and the policy facts, all through tracked
reads, and is charged its locally counted tokens. Systems differ only in when pages are rebuilt
and whether out-of-date pages are served. At every session a sample of the world's questions is
recalled (no answering) so read counts exist for the schedulers; at checkpoints every question
with gold is answered from the system's context with the dataset's answer prompt.

Commands:
  gold     compile checkpoint gold with the dataset oracle (no model calls)
  extract  fact extraction per world
  simulate run the systems and write contexts
  answer   answer every context
  report   score and write results/memderive_<tag>.{json,md}
  all      everything above in order
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import random
import re
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from lme_models import BackendError, CachedBackend, ResponseStore, make_backend, request_tokens
from longmemeval import cluster_bootstrap, rate, read_json, run_pool, write_json

from strata import Budget, Context, Event, FactWrite, Memory, Policy, StrataError
from strata.llm import ModelRequest
from strata.scheduler import key as policy_key

HERE = Path(__file__).resolve().parent
DERIVEMEM = Path(os.environ.get("DERIVEMEM", HERE / "data" / "derivemem"))
sys.path.insert(0, str(DERIVEMEM))
try:
    from benchmark.core.checkpoints import compile_gold, place_checkpoints
    from benchmark.scoring.provenance import PROVENANCE_V2, score_provenance
    from benchmark.scoring.score import ANSWER_LABELS, WITNESS_THETA, answer_matches
except ImportError:  # reported by main(); the dataset checkout is not vendored
    compile_gold = None

OUT = HERE / "out" / "memderive"
RESULTS = HERE / "results"

DEV_WORLDS = ("tripops_w1", "shopops_w1", "eduops_w1")
HELDOUT_WORLDS = (
    "tripops_w2",
    "tripops_w3",
    "tripops_w4",
    "shopops_w2",
    "shopops_w3",
    "eduops_w2",
    "eduops_w3",
)
FRACTIONS = (0.2, 0.4, 0.6, 0.8, 1.0)
UNANSWERABLE = "unanswerable_incomplete"  # gold label for an incomplete record
# Extraction and page writing. Existing factlogs and contexts were built with it; keep it so the
# response cache stays valid.
MODEL = "cursor:composer-2.5-fast"
ANSWERERS = ("cursor:composer-2.5", "cursor:grok-4.7-low")

GROUP_SESSIONS = 6  # sessions per extraction call
GROUP_CHARS = 5000
SUBJECT_LIST_CHARS = 9000
POLICY = "policy"
PAGE_KIND = "page"
PAGE_MIN_FACTS = 2  # a subject gets a page once it has this many live facts
PAGE_DECLARED_COST = 2500.0  # tokens, until a page is built (budgeted systems: eager mean)
LINKED_LIMIT = 10
LINKED_CHARS = 700
RECALL_LIMIT = 10
EVENT_ITEMS = 25
CONTEXT_CHARS = 16000
READ_FRACTION = 0.1  # questions recalled (not answered) after every session
LAZY_BUDGET = 6000.0  # tokens per query
PERIODIC_EVERY = 10  # sessions between full consolidations
BUDGET_FRACTIONS = (0.15, 0.4)  # of eager's mean tokens per session
PERSONAS = {
    "tripops_w1": ("a traveler named Vera", "trips"),
    "tripops_w2": ("a traveler named Joren", "trips"),
    "tripops_w3": ("a traveler named Petra", "trips"),
    "tripops_w4": ("a traveler named Soren", "trips"),
    "shopops_w1": ("a shopper named Rina", "orders"),
    "shopops_w2": ("a shopper named Dara", "orders"),
    "shopops_w3": ("a shopper named Wren", "orders"),
    "eduops_w1": ("a student named Milo", "courses"),
    "eduops_w2": ("a student named Isla", "courses"),
    "eduops_w3": ("a student named Teo", "courses"),
}

CALL_GATE = threading.Semaphore(32)  # concurrent model calls across all threads
LOCK = threading.Lock()


# ---------------------------------------------------------------------------- systems


@dataclass(frozen=True)
class SystemSpec:
    name: str
    context: str  # "full" | "oracle" | "bm25" | "strata"
    pages: bool = False
    schedule: str = "none"  # none | eager | rebuild | periodic | untracked | budget | lazy
    policy: str = "fifo"
    budget_fraction: float | None = None
    serve_stale: bool = False  # out-of-date pages served as current (no tracking / control)
    answered: bool = True
    checkpoints: tuple[str, ...] | None = None  # None = every checkpoint
    note: str = ""


def _budget_systems() -> list[SystemSpec]:
    out = []
    for fraction in BUDGET_FRACTIONS:
        pct = round(fraction * 100)
        for policy in ("fifo", "priority", "demand"):
            out.append(
                SystemSpec(
                    f"{policy}_{pct}",
                    "strata",
                    True,
                    "budget",
                    policy,
                    fraction,
                    note=(
                        f"incremental, {pct}% of eager's mean tokens per session with unused "
                        f"budget carried over, {policy} order"
                    ),
                )
            )
    return out


SYSTEMS: tuple[SystemSpec, ...] = (
    SystemSpec("full_context", "full", note="whole log up to the checkpoint"),
    SystemSpec("oracle", "oracle", checkpoints=("p100",), note="gold support events only"),
    SystemSpec("bm25", "bm25", note="BM25 over raw events, same context budget"),
    SystemSpec("facts", "strata", note="Strata facts and events, no pages"),
    SystemSpec("eager", "strata", True, "eager", note="incremental, unlimited budget"),
    SystemSpec(
        "full_rebuild",
        "strata",
        True,
        "rebuild",
        answered=False,
        note="every page rebuilt after every session (full consolidation)",
    ),
    SystemSpec(
        "periodic",
        "strata",
        True,
        "periodic",
        serve_stale=True,
        note=f"full consolidation every {PERIODIC_EVERY} sessions, pages served in between",
    ),
    SystemSpec(
        "untracked",
        "strata",
        True,
        "untracked",
        serve_stale=True,
        note="rebuild only pages of subjects named in the new session (no dependency tracking)",
    ),
    SystemSpec("lazy", "strata", True, "lazy", note=f"repair on read, {LAZY_BUDGET:.0f} tokens"),
    *_budget_systems(),
    SystemSpec(
        "stale_priority_15",
        "strata",
        True,
        "budget",
        "priority",
        0.15,
        serve_stale=True,
        note="priority_15 but out-of-date pages served as current (safety control)",
    ),
)
SPECS = {s.name: s for s in SYSTEMS}


# ---------------------------------------------------------------------------- world data


@dataclass
class World:
    name: str
    events: list[dict[str, Any]]
    sessions: list[tuple[str, list[dict[str, Any]]]]
    checkpoints: list[dict[str, Any]]
    gold: dict[str, dict[str, dict[str, Any]]]  # checkpoint -> qid -> gold row
    questions: dict[str, dict[str, str]]  # checkpoint -> qid -> question text
    explicit: dict[str, dict[str, bool]]  # checkpoint -> qid -> change named the query's args
    query_ids: list[str] = field(default_factory=list)

    def boundary(self, checkpoint: str) -> int:
        """Number of completed sessions at a checkpoint."""
        return next(
            c["completed_sessions"] for c in self.checkpoints if c["checkpoint_id"] == checkpoint
        )


def _mentions(event: Any, args: Iterable[Any]) -> bool:
    text = repr(event.actions)
    return any(str(a) in text for a in args if str(a))


def compile_world_gold(world: str) -> dict[str, Any]:
    path = OUT / "gold" / f"{world}.json"
    if path.exists():
        return read_json(path)
    scenario = importlib.import_module(f"benchmark.scenarios.{world}.dataset")
    events = scenario.build_events()
    by_id = {e.event_id: e for e in events}
    checkpoints = place_checkpoints(events, scenario.AS_OF, FRACTIONS)
    end = scenario.AS_OF.replace("T", " ")
    out: dict[str, Any] = {"checkpoints": checkpoints, "gold": {}, "questions": {}, "explicit": {}}
    previous: dict[str, Any] | None = None
    for ck in checkpoints:
        cid = ck["checkpoint_id"]
        rows = compile_gold(
            events,
            ck["as_of"],
            scenario.QUERIES,
            scenario.EVALUATORS,
            scenario.RESOLVER,
            scenario.ANSWER_ALIASES,
        )
        out["gold"][cid] = {q: r for q, r in rows.items() if r is not None}
        stamp = ck["as_of"].replace("T", " ")
        out["questions"][cid] = {s.qid: s.question.replace(end, stamp) for s in scenario.QUERIES}
        out["explicit"][cid] = {}
        if previous is not None:
            for spec in scenario.QUERIES:
                now, before = out["gold"][cid].get(spec.qid), previous.get(spec.qid)
                if now is None or before is None or gold_state(now) == gold_state(before):
                    continue
                added = set(now["valid_support"]) - set(before["valid_support"])
                out["explicit"][cid][spec.qid] = any(
                    _mentions(by_id[e], spec.args) for e in added if e in by_id
                )
        previous = out["gold"][cid]
    write_json(path, out)
    return out


def gold_state(row: dict[str, Any]) -> str:
    return json.dumps([row["answerability"], row["gold_answer"]], sort_keys=True, default=str)


def load_world(world: str) -> World:
    data = DERIVEMEM / "benchmark" / "data" / world
    events = [json.loads(x) for x in (data / "chronicle.jsonl").read_text().splitlines() if x]
    sessions: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        sessions.setdefault(e["session_id"], []).append(e)
    g = compile_world_gold(world)
    lines = (data / "queries.jsonl").read_text().splitlines()
    query_ids = [json.loads(x)["query_id"] for x in lines if x.strip()]
    return World(
        world,
        events,
        list(sessions.items()),
        g["checkpoints"],
        g["gold"],
        g["questions"],
        g["explicit"],
        query_ids,
    )


_AS_OF = re.compile(r"^As of \d{4}-\d\d-\d\d \d\d:\d\d,\s*")


def search_text(question: str) -> str:
    """The question without its "As of <time>," prefix, which only adds noise to search."""
    return _AS_OF.sub("", question)


def event_line(e: dict[str, Any]) -> str:
    return f"{e['event_id']} | {e['time']} | {e['source']} | {e['text']}"


def event_time(raw: str) -> str:
    return datetime.fromisoformat(raw.replace(" ", "T")).isoformat()


# ---------------------------------------------------------------------------- extraction

EXTRACT_SYSTEM = (
    "You turn an activity log into a versioned fact memory. You get the memory's existing "
    "subjects and new log entries (format: event_id | time | source | text). Reply with one JSON "
    'object only, no prose: {"facts": [{"event": "<event_id>", "subject": str, "attribute": '
    'str, "value": <string, number, boolean or list; never null>, "op": "set" | "add" | '
    '"remove" | "retract"}]}\n'
    "Rules:\n"
    "- subject: the record the fact belongs to. Use the main business record when the entry "
    'concerns one (a trip, order, course, claim, case, e.g. "Trip-Grus" or "Order-1042"); a '
    'named event, place, person or product otherwise; and "policy" for general rules, rates, '
    "fees, thresholds, allowances, tiers, deadlines and processes. When an entry concerns an "
    "item that belongs to a record (a booking, receipt, refund, expense, item, assignment, "
    "grade), file the fact under that record (e.g. Trip-Grus.receipts). Reuse names from "
    "EXISTING SUBJECTS exactly; never create a second name for the same thing.\n"
    "- attribute: short snake_case. Reuse an existing attribute of the subject when the entry "
    "updates the same property, so the new value replaces the old one.\n"
    '- op "set" replaces a single-valued property (a status, amount, date, rule); "add" appends '
    'one item to a collection (bookings, receipts, expenses, items, members); "remove" deletes '
    'an item added before (give it exactly as it was added); "retract" says a property no '
    "longer holds.\n"
    "- value: self-contained: keep every name, amount, currency, quantity, date and status the "
    "entry gives. For a policy fact the value is the full rule.\n"
    "- Corrections, cancellations, refunds, replacements, completions and status changes are "
    "updates: set the new state, or remove the item.\n"
    "- Skip small talk, opinions, jokes, reminders and newsletters that state nothing durable; "
    "cover every entry that does."
)


def subject_listing(m: Memory, order: list[str]) -> str:
    lines, used = [], 0
    for subject in reversed(order):
        facts = m.facts(subject)
        if not facts:
            continue
        parts = [f"{f.attribute}={json.dumps(f.value, ensure_ascii=False)[:50]}" for f in facts]
        line = f"{subject}: " + "; ".join(parts)[:400]
        if used + len(line) > SUBJECT_LIST_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(reversed(lines)) or "none"


def make_groups(world: World) -> list[list[tuple[str, list[dict[str, Any]]]]]:
    groups: list[list[tuple[str, list[dict[str, Any]]]]] = []
    chars = 0
    for sid, events in world.sessions:
        size = sum(len(event_line(e)) for e in events)
        if not groups or len(groups[-1]) >= GROUP_SESSIONS or chars + size > GROUP_CHARS:
            groups.append([])
            chars = 0
        groups[-1].append((sid, events))
        chars += size
    return groups


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def json_object(text: str) -> Any:
    body = text.strip()
    fenced = _FENCE.search(body)
    if fenced:
        body = fenced.group(1)
    start = body.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    return json.JSONDecoder().raw_decode(body, start)[0]


def parse_facts(text: str, event_ids: set[str]) -> list[dict[str, Any]]:
    data = json_object(text)
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        raise ValueError('reply has no "facts" list')
    out = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        op = f.get("op", "set")
        subject, attribute = f.get("subject"), f.get("attribute")
        if f.get("event") not in event_ids or op not in ("set", "add", "remove", "retract"):
            continue
        if not isinstance(subject, str) or not isinstance(attribute, str):
            continue
        if not subject.strip() or not attribute.strip() or subject.startswith("_"):
            continue
        if f.get("value") is None and op != "retract":
            continue
        out.append(
            {
                "event": f["event"],
                "subject": subject.strip(),
                "attribute": attribute.strip(),
                "value": f.get("value"),
                "op": op,
            }
        )
    return out


def extract_world(world: World, model: CachedBackend) -> dict[str, Any]:
    """Facts per session, extracted group by group with the memory built so far as context."""
    path = OUT / "factlogs" / f"{world.name}.json"
    if path.exists():
        return read_json(path)
    per_session: dict[str, list[dict[str, Any]]] = {sid: [] for sid, _ in world.sessions}
    ledger = Counter()
    failed: list[str] = []
    order: list[str] = []  # subjects, least recently written first
    with tempfile.TemporaryDirectory() as tmp, Memory.open(str(Path(tmp) / "x.db")) as m:
        for group in make_groups(world):
            for sid, events in group:
                m.ingest(
                    sid, [Event(e["event_id"], sid, "log", {"text": e["text"]}) for e in events]
                )
            ids = {e["event_id"] for _, events in group for e in events}
            entries = "\n".join(event_line(e) for _, events in group for e in events)
            prompt = (
                f"EXISTING SUBJECTS:\n{subject_listing(m, order)}\n\nNEW LOG ENTRIES:\n{entries}"
            )
            facts: list[dict[str, Any]] | None = None
            error = ""
            for _attempt in range(3):
                request = ModelRequest(
                    prompt
                    + (
                        f"\n\nYour previous reply was rejected ({error}). Reply again "
                        "with corrected JSON only."
                        if error
                        else ""
                    ),
                    EXTRACT_SYSTEM,
                    max_tokens=6000,
                )
                with CALL_GATE:
                    rec = model.call(request)
                ledger["calls"] += 1
                ledger["tokens"] += request_tokens(request, rec.text)
                try:
                    facts = parse_facts(rec.text, ids)
                    break
                except (ValueError, json.JSONDecodeError) as exc:
                    error = str(exc)[:200]
            if facts is None:
                failed.extend(sid for sid, _ in group)
                continue
            session_of = {e["event_id"]: sid for sid, events in group for e in events}
            for f in facts:
                per_session[session_of[f["event"]]].append(f)
                writes = fact_writes([f])
                try:
                    m.assert_facts(writes)
                except StrataError:
                    continue
                if f["subject"] in order:
                    order.remove(f["subject"])
                order.append(f["subject"])
    log = {"world": world.name, "sessions": per_session, "failed": failed, **ledger}
    write_json(path, log)
    return log


def fact_writes(facts: list[dict[str, Any]]) -> list[FactWrite]:
    return [
        FactWrite(f["subject"], f["attribute"], f["value"], f["event"], mode=f["op"]) for f in facts
    ]


# ---------------------------------------------------------------------------- pages

PAGE_PROMPT = (
    'You maintain one page of an assistant\'s memory: the page for "{subject}". Below are the '
    "dated facts recorded about it (oldest first, each with the log event that stated it), facts "
    "about the records it names, and the policies. Write the page: first the current state of "
    "{subject} (every current value, item and status, with amounts, currencies and dates), then "
    "what follows from applying the current policies to it (totals, eligibility, requirements, "
    "caps, conversions, flags), then a short history of what changed. Mark superseded, canceled, "
    "refunded or corrected values as such; never state them as current. Cite the supporting "
    "event ids in square brackets after each statement, e.g. [{example}]. Plain text, at most "
    "300 words. Use only what is given below.\n\n"
    "FACTS ABOUT {subject} (oldest first):\n{own}\n\nRECORDS IT NAMES:\n{linked}\n\n"
    "POLICIES (oldest first; a later entry replaces an earlier one of the same name):\n{policy}"
)
POLICY_PROMPT = (
    "You maintain the policy page of an assistant's memory. Below are the dated policy facts "
    "(oldest first, each with the log event that stated it; a later entry replaces an earlier "
    "one of the same name). Write the page: every rule, rate, threshold and process now in "
    "force, then what each replaced and when, so that a reader can tell which rule applied on "
    "any date. Keep every number and condition. Cite event ids in square brackets after each "
    "statement, e.g. [{example}]. Plain text, at most 400 words. Use only what is given.\n\n"
    "POLICY FACTS (oldest first):\n{policy}"
)
_NAME_TOKEN = re.compile(r"[A-Za-z0-9][\w'-]*")


def timeline(history: dict[str, list[dict[str, Any]]], prefix: str = "") -> list[str]:
    """Dated change lines for one subject's slot histories, oldest first."""
    entries: list[tuple[str, str, str]] = []
    for attribute, changes in history.items():
        before: Any = None
        for c in changes:
            at = (c["at"] or "")[:16].replace("T", " ")
            head = f"[{at} {c['source']}] {prefix}{attribute}"
            value = c["value"] if c["op"] == "assert" else None
            if isinstance(value, list) and isinstance(before, list):
                for item in value:
                    if item not in before:
                        entries.append((at, c["source"], f"{head} + {_show(item)}"))
                for item in before:
                    if item not in value:
                        entries.append((at, c["source"], f"{head} - {_show(item)} (removed)"))
            elif c["op"] == "assert":
                entries.append((at, c["source"], f"{head} = {_show(value)}"))
            else:
                entries.append((at, c["source"], f"{head} no longer holds"))
            before = value
    return [line for _, _, line in sorted(entries)]


def _show(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def name_candidates(values: Iterable[Any]) -> list[str]:
    """Capitalised word n-grams (up to four words) of the given values: possible subject names."""
    found: dict[str, None] = {}
    for value in values:
        tokens = _NAME_TOKEN.findall(_show(value))
        for i, token in enumerate(tokens):
            if not (token[0].isupper() or token[0].isdigit() or "-" in token):
                continue
            for n in range(1, 5):
                if i + n <= len(tokens):
                    found[" ".join(tokens[i : i + n])] = None
    return list(found)


class PageWriter:
    """Derivation of one page node; charged the page call's locally counted tokens."""

    def __init__(self, model: CachedBackend, example_id: str, ledger: Counter[str]) -> None:
        self.model = model
        self.example = example_id
        self.ledger = ledger

    def request(self, ctx: Context) -> ModelRequest:
        subject = ctx.key
        policy = timeline(ctx.subject_history(POLICY))
        if subject == POLICY:
            prompt = POLICY_PROMPT.format(
                example=self.example, policy="\n".join(policy) or "(none yet)"
            )
            return ModelRequest(prompt, max_tokens=1200)
        history = ctx.subject_history(subject)
        values = [c["value"] for changes in history.values() for c in changes]
        linked = []
        for name in name_candidates(values):
            if name in (subject, POLICY) or len(linked) >= LINKED_LIMIT:
                continue
            facts = ctx.subject(name)  # tracked, also when the subject does not exist yet
            if facts:
                body = "; ".join(f"{a} = {_show(v)}" for a, v in sorted(facts.items()))
                linked.append(f"{name}: {body}"[:LINKED_CHARS])
        prompt = PAGE_PROMPT.format(
            subject=subject,
            example=self.example,
            own="\n".join(timeline(history)) or "(none)",
            linked="\n".join(linked) or "(none)",
            policy="\n".join(policy) or "(none yet)",
        )
        return ModelRequest(prompt, max_tokens=900)

    def __call__(self, ctx: Context) -> str:
        request = self.request(ctx)
        with CALL_GATE:
            rec = self.model.call(request)
        tokens = request_tokens(request, rec.text)
        ctx.charge(tokens)
        with LOCK:
            self.ledger["calls"] += 1
            self.ledger["live_calls"] += int(not rec.cached)
            self.ledger["tokens"] += tokens
        return rec.text.strip()


def page_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


PREFETCH_POOL = ThreadPoolExecutor(max_workers=48)


def prefetch(m: Memory, writer: PageWriter, rows: list[Any]) -> None:
    """Warm the response cache for pages about to be rebuilt, in parallel.

    The engine rebuilds nodes one at a time; computing their requests here (same reads, so the
    same prompts) and calling the model concurrently makes the rebuild itself hit the cache.
    """
    requests = []
    for row in rows:
        ctx = Context(m.store, int(row["node_id"]), row["kind"], row["node_key"], int(row["level"]))
        requests.append(writer.request(ctx))

    def warm(request: ModelRequest) -> None:
        with CALL_GATE, contextlib.suppress(BackendError):  # the rebuild retries and records it
            writer.model.call(request)

    list(PREFETCH_POOL.map(warm, requests))


def dirty_rows(m: Memory) -> list[Any]:
    return m.engine._candidates()


def planned(rows: list[Any], policy: str, budget: float) -> list[Any]:
    """The nodes a budgeted session rebuilds: the longest policy-order prefix that fits.

    The engine's own budget handling is first fit (a node that does not fit is skipped and later
    ones still run). With credit carried across sessions that turns every policy into
    cheapest-first, so the benchmark stops at the first node that does not fit and saves credit.
    """
    chosen, spent = [], 0.0
    for row in sorted(rows, key=policy_key(Policy(policy))):
        estimate = row["cost"] if row["measured_cost"] is None else row["measured_cost"]
        if spent + estimate > budget:
            break
        chosen.append(row)
        spent += estimate
    return chosen


# ---------------------------------------------------------------------------- contexts


def render(blocks: list[str]) -> tuple[str, int]:
    parts, used = [], 0
    for block in blocks:
        if used + len(block) > CONTEXT_CHARS:
            continue
        parts.append(block)
        used += len(block)
    return "\n".join(parts), len(parts)


def raw_context(world: World, spec: SystemSpec, m: Memory | None, cid: str, qid: str) -> str:
    done = world.boundary(cid)
    visible = [e for _, events in world.sessions[:done] for e in events]
    if spec.context == "full":
        return "\n".join(event_line(e) for e in visible)
    order = {e["event_id"]: i for i, e in enumerate(world.events)}
    by_id = {e["event_id"]: e for e in world.events}
    if spec.context == "oracle":
        ids = sorted(world.gold[cid][qid]["valid_support"], key=order.__getitem__)
        return "\n".join(event_line(by_id[i]) for i in ids if i in by_id)
    assert m is not None
    hits = m.search_events(search_text(world.questions[cid][qid]), 400)
    chosen, used = [], 0
    for h in hits:
        line = event_line(by_id[h.key])
        if used + len(line) + 1 > CONTEXT_CHARS:
            continue
        chosen.append(h.key)
        used += len(line) + 1
    return "\n".join(event_line(by_id[k]) for k in sorted(chosen, key=order.__getitem__))


def strata_context(
    m: Memory, spec: SystemSpec, question: str, by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    res = m.recall(
        search_text(question),
        limit=RECALL_LIMIT,
        include_stale=spec.serve_stale,
        lazy_budget=LAZY_BUDGET if spec.schedule == "lazy" else None,
        events=EVENT_ITEMS,
    )
    items = list(res.items)
    if spec.serve_stale:
        # recall lists stale pages after up to RECALL_LIMIT current items. A system without
        # freshness tracking cannot tell them apart, so they compete for the same slots by score.
        ranked = sorted(
            (i for i in items if i.status in ("current", "stale")), key=lambda i: -i.score
        )
        items = ranked[:RECALL_LIMIT] + [i for i in items if i.status not in ("current", "stale")]
    blocks, statuses = [], Counter()
    for item in items:
        sources = [p.removeprefix("event:") for p in item.provenance if p.startswith("event:")]
        if item.kind == "event":
            blocks.append(f"[log] {event_line(by_id[item.key])}")
        elif item.kind == "fact":
            label = "fact" if item.status == "current" else "fact, UNVERIFIED"
            when = f", {item.at[:16].replace('T', ' ')}" if item.at else ""
            blocks.append(f"[{label}] {item.text} (source: {', '.join(sources)}{when})")
        else:
            shown = sorted(sources, key=lambda k: by_id[k]["time"] if k in by_id else "")
            blocks.append(
                f"[memory page: {item.key}] (sources: {', '.join(shown[-40:])})\n{item.value}"
            )
        statuses[f"{item.status}:{item.kind if item.kind in ('event', 'fact') else 'page'}"] += 1
    rendered, fitted = render(blocks)
    return {
        "rendered": rendered,
        "items": fitted,
        "statuses": dict(statuses),
        "fallback": res.fallback_used,
        "suppressed": res.suppressed,
        "refreshed": res.refreshed,
    }


# ---------------------------------------------------------------------------- simulation


def page_hashes(m: Memory) -> dict[str, str]:
    return {
        n.key: hashlib.sha256(json.dumps(n.value).encode()).hexdigest()[:16]
        for n in m.nodes()
        if n.kind == PAGE_KIND and n.state == "valid"
    }


def simulate(
    world: World,
    spec: SystemSpec,
    factlog: dict[str, Any],
    model: CachedBackend,
    budget: float,
    declared_cost: float = PAGE_DECLARED_COST,
) -> dict[str, Any]:
    path = OUT / "contexts" / f"{world.name}__{spec.name}.json"
    if path.exists():
        return read_json(path)
    by_id = {e["event_id"]: e for e in world.events}
    boundaries = {c["completed_sessions"]: c["checkpoint_id"] for c in world.checkpoints}
    end_questions = world.questions[world.checkpoints[-1]["checkpoint_id"]]
    ledger: Counter[str] = Counter()
    writer = PageWriter(model, world.events[0]["event_id"], ledger)
    contexts: dict[str, dict[str, Any]] = {}
    hashes: dict[str, dict[str, str]] = {}
    trace: list[dict[str, Any]] = []
    assert_failures = 0
    started = time.time()
    # A budgeted system earns ``budget`` per session and keeps what it does not spend, so a
    # page that costs more than one session's allowance is still built once enough accrues.
    # A run may overshoot by one job's misestimate; the debt is repaid from later sessions.
    credit = 0.0
    with tempfile.TemporaryDirectory() as tmp, Memory.open(str(Path(tmp) / "m.db")) as m:
        defined: set[str] = set()
        for number, (sid, events) in enumerate(world.sessions, 1):
            m.ingest(
                sid,
                [
                    Event(
                        e["event_id"],
                        sid,
                        e["source"],
                        {"text": e["text"], "source": e["source"], "time": e["time"]},
                        event_time(e["time"]),
                    )
                    for e in events
                ],
            )
            touched: set[str] = set()
            if spec.context == "strata":
                facts = factlog["sessions"].get(sid, [])
                try:
                    m.assert_facts(fact_writes(facts))
                except StrataError:
                    for f in facts:  # find and skip the bad write
                        try:
                            m.assert_facts(fact_writes([f]))
                        except StrataError:
                            assert_failures += 1
                touched = {f["subject"] for f in facts}
            if spec.pages:
                for subject in sorted(touched - defined):
                    if subject == POLICY or len(m.facts(subject)) >= PAGE_MIN_FACTS:
                        m.define(
                            PAGE_KIND,
                            subject,
                            1,
                            writer,
                            declared_cost,
                            search_text=page_text,
                            keywords=subject.replace("-", " "),
                        )
                        defined.add(subject)
                credit += budget
                credit -= maintain(m, spec, writer, number, touched, max(credit, 0.0))
            if spec.context == "strata":
                rng = random.Random(f"{world.name}:{number}")
                sample = rng.sample(
                    world.query_ids, max(1, round(READ_FRACTION * len(end_questions)))
                )
                for qid in sample:
                    m.recall(
                        search_text(end_questions[qid]),
                        limit=RECALL_LIMIT,
                        lazy_budget=LAZY_BUDGET if spec.schedule == "lazy" else None,
                        events=0,
                    )
            cid = boundaries.get(number)
            if cid is None or (spec.checkpoints is not None and cid not in spec.checkpoints):
                continue
            states = m.status()["nodes"]
            trace.append(
                {
                    "checkpoint": cid,
                    "nodes": states,
                    "cost_so_far": m.engine.totals.cost,
                    "tokens_so_far": ledger["tokens"],
                }
            )
            hashes[cid] = page_hashes(m)
            contexts[cid] = {}
            for qid in world.gold[cid]:
                question = world.questions[cid][qid]
                if spec.context == "strata":
                    contexts[cid][qid] = strata_context(m, spec, question, by_id)
                else:
                    rendered = raw_context(world, spec, m, cid, qid)
                    contexts[cid][qid] = {"rendered": rendered}
        totals = m.engine.totals
        db_bytes = sum(p.stat().st_size for p in Path(tmp).glob("m.db*"))
        result = {
            "world": world.name,
            "system": spec.name,
            "contexts": contexts,
            "page_hashes": hashes,
            "trace": trace,
            "maintenance": {k: v for k, v in totals.as_dict().items() if k != "outcomes"},
            "page_calls": ledger["calls"],
            "page_live_calls": ledger["live_calls"],
            "page_tokens": ledger["tokens"],
            "budget_per_session": budget if spec.schedule == "budget" else None,
            "assert_failures": assert_failures,
            "pages_defined": len(defined),
            "status": m.status(),
            "db_bytes": db_bytes,
            "seconds": time.time() - started,
        }
    write_json(path, result)
    return result


def maintain(
    m: Memory, spec: SystemSpec, writer: PageWriter, number: int, touched: set[str], budget: float
) -> float:
    """Run this session's maintenance; return the budgeted cost spent (0 when unbudgeted)."""
    schedule = spec.schedule
    if schedule == "eager":
        prefetch(m, writer, dirty_rows(m))
        m.maintain(None, "fifo")
    elif schedule == "rebuild" or (schedule == "periodic" and number % PERIODIC_EVERY == 0):
        m.engine.mark_all_stale()
        prefetch(m, writer, dirty_rows(m))
        m.maintain(None, "fifo")
    elif schedule == "untracked":
        names = touched | ({POLICY} if POLICY in touched else set())
        rows = [r for r in dirty_rows(m) if r["node_key"] in names]
        prefetch(m, writer, rows)
        m.engine.maintain(None, "fifo", only={int(r["node_id"]) for r in rows})
    elif schedule == "budget":
        rows = planned(dirty_rows(m), spec.policy, budget)
        prefetch(m, writer, rows)
        only = {int(r["node_id"]) for r in rows}
        return m.engine.maintain(Budget(budget), spec.policy, only=only).cost
    return 0.0


# ---------------------------------------------------------------------------- answering

READER_COMPLETE = {
    "oracle": (
        " These excerpts are the complete set of events relevant to this question; no other "
        "relevant event exists in the log, and the latest excerpt about a fact reflects its state "
        "as of the question time. Context completeness is not a records-sync declaration: "
        "closed_world_zero still requires an explicit complete/synced event among the excerpts."
    ),
    "full": (
        " This is the complete activity log through the question time; no event at or before "
        "that time exists outside it, and the latest entry about a fact reflects its state as of "
        "the question time."
    ),
}
LABELS = "answerable | closed_world_zero | unanswerable_incomplete"


def answer_prompt(world: str, spec: SystemSpec, cid: str, qid: str, question: str, ctx: str) -> str:
    """DeriveMem's v9 answering prompt (DerivEval prompt_catalog/answer_derivemem.txt)."""
    persona, unit = PERSONAS[world]
    if spec.context == "strata":
        block = (
            "A memory system has stored the log; below are the memories it retrieved for this "
            "question (each lists its source event ids):\n" + (ctx or "(no memories retrieved)")
        )
        judge = "Judge only from the retrieved memories above."
    else:
        block = (
            "Activity log excerpts (chronological, format: event_id | time | source | text):\n"
            + (ctx or "(No activity-log excerpt was retrieved.)")
        )
        judge = "Judge only from the log excerpts above." + READER_COMPLETE.get(spec.context, "")
    as_of = world_as_of(world, cid)
    return (
        f"You assist with questions about a long-running work activity log of {persona}. The log "
        f"uses fictional places and internal company policies.\n\n{block}\n\n"
        f"Question ({qid}), to be answered as of {as_of}:\n{question}\n\nAnswering rules:\n"
        f"- {judge} Do not rely on outside world knowledge; places and policies here are "
        f'fictional.\n- "answerability" must be one of: {LABELS}.\n'
        "  * answerable: the information supports a definite answer.\n"
        "  * closed_world_zero: the answer is zero/empty AND the relevant records are explicitly "
        "declared complete or fully synced. Mere absence of mentions is not enough.\n"
        "  * unanswerable_incomplete: the needed records are missing or explicitly incomplete, "
        'so no exact answer is possible; set "answer" to null.\n'
        f"- Keep different {unit} apart, and do not trust facts that later information shows "
        "were superseded (canceled, corrected, replaced).\n"
        "- Booleans as true/false, totals as plain numbers, dates as YYYY-MM-DD, lists as JSON "
        "arrays.\n"
        '- "witness_ids": the minimal set of event ids that prove the answer. Include the '
        "policy/rule events you relied on and the events showing that superseded facts no longer "
        "hold. Do not cite stale or merely related events.\n\n"
        "Respond with a single JSON object and nothing else:\n"
        f'{{"query_id": "{qid}", "answerability": "...", "answer": ..., "witness_ids": [...], '
        '"reasoning": "..."}'
    )


_AS_OF_CACHE: dict[tuple[str, str], str] = {}


def world_as_of(world: str, cid: str) -> str:
    key = (world, cid)
    if key not in _AS_OF_CACHE:
        g = read_json(OUT / "gold" / f"{world}.json")
        for c in g["checkpoints"]:
            _AS_OF_CACHE[(world, c["checkpoint_id"])] = c["as_of"].replace("T", " ")
    return _AS_OF_CACHE[key]


def parse_answer(text: str, qid: str) -> dict[str, Any]:
    try:
        row = json_object(text)
    except (ValueError, json.JSONDecodeError):
        row = {}
    if not isinstance(row, dict):
        row = {}
    witness = row.get("witness_ids")
    return {
        "query_id": qid,
        "answerability": row.get("answerability"),
        "answer": row.get("answer"),
        "witness_ids": [str(w) for w in witness] if isinstance(witness, list) else [],
        "parsed": bool(row),
    }


def answer_all(
    jobs: list[tuple[World, SystemSpec]], answerer: CachedBackend, workers: int
) -> list[str]:
    """Answer every context of the given (world, system) pairs in one pool."""
    files: dict[tuple[str, str], tuple[Path, dict[str, Any]]] = {}
    tasks = []
    for world, spec in jobs:
        source = OUT / "contexts" / f"{world.name}__{spec.name}.json"
        if not source.exists():
            continue
        path = OUT / "answers" / f"{world.name}__{spec.name}__{slug(answerer.name)}.json"
        done: dict[str, Any] = read_json(path) if path.exists() else {}
        files[(world.name, spec.name)] = (path, done)
        for cid, by_q in read_json(source)["contexts"].items():
            for qid, c in by_q.items():
                if f"{cid}/{qid}" not in done:
                    tasks.append((world, spec, cid, qid, c["rendered"], done))

    def run(task: tuple[World, SystemSpec, str, str, str, dict[str, Any]]) -> None:
        world, spec, cid, qid, rendered, done = task
        question = world.questions[cid][qid]
        request = ModelRequest(
            answer_prompt(world.name, spec, cid, qid, question, rendered), max_tokens=1500
        )
        with CALL_GATE:
            rec = answerer.call(request)
        row = parse_answer(rec.text, qid)
        row["tokens"] = request_tokens(request, rec.text)
        row["latency_s"] = rec.latency_s
        with LOCK:
            done[f"{cid}/{qid}"] = row

    _, errors = run_pool(f"answer {answerer.name}", workers, tasks, run)
    for path, done in files.values():
        write_json(path, done)
    return errors


def slug(spec: str) -> str:
    return spec.replace(":", "_").replace("/", "_")


# ---------------------------------------------------------------------------- scoring


def correct_against(pred: dict[str, Any], gold: dict[str, Any]) -> bool:
    if gold["answerability"] in ANSWER_LABELS:
        return answer_matches(pred.get("answer"), gold["gold_answer"]) or any(
            answer_matches(pred.get("answer"), alias) for alias in gold.get("accepted_aliases", [])
        )
    return pred.get("answerability") == gold["answerability"]


_EVENT_ID = re.compile(r"\b[a-z]*\d*_?e\d{3,4}\b")


def score_cells(world: World, spec: SystemSpec, answers: dict[str, Any]) -> list[dict[str, Any]]:
    ctx = read_json(OUT / "contexts" / f"{world.name}__{spec.name}.json")
    order = [c["checkpoint_id"] for c in world.checkpoints]
    cells = []
    for cid, by_q in ctx["contexts"].items():
        prev_cid = order[order.index(cid) - 1] if order.index(cid) > 0 else None
        for qid, c in by_q.items():
            pred = answers.get(f"{cid}/{qid}")
            if pred is None:
                continue
            gold = world.gold[cid][qid]
            ok = correct_against(pred, gold)
            prov = score_provenance(set(pred["witness_ids"]), gold, PROVENANCE_V2)
            support = set(gold["valid_support"])
            shown = set(_EVENT_ID.findall(c["rendered"]))
            prev = world.gold.get(prev_cid, {}).get(qid) if prev_cid else None
            changed = prev is not None and gold_state(prev) != gold_state(gold)
            # A previous-gold match is an old-value answer only if the previous gold was a value;
            # matching a previous unanswerable_incomplete is an abstention, not staleness.
            prev_ok = bool(changed and not ok and correct_against(pred, prev))
            prev_value = prev is not None and prev["answerability"] != UNANSWERABLE
            cells.append(
                {
                    "world": world.name,
                    "checkpoint": cid,
                    "qid": qid,
                    "family": gold["family"],
                    "label": gold["answerability"],
                    "correct": ok,
                    "abstained": pred["answerability"] == UNANSWERABLE,
                    "witnessed": ok
                    and prov["precision"] >= WITNESS_THETA
                    and prov["recall"] >= WITNESS_THETA,
                    "witness_f1": prov["f1"],
                    "support_recall": len(support & shown) / len(support) if support else 1.0,
                    "has_prev": prev is not None,
                    "changed": changed,
                    "explicit": world.explicit.get(cid, {}).get(qid) if changed else None,
                    "value_change": changed
                    and prev_value
                    and gold["answerability"] != UNANSWERABLE,
                    "old_value": prev_ok and prev_value,
                    "stale_abstention": prev_ok and not prev_value,
                    "tokens": pred["tokens"],
                    "stale_served": sum(
                        n for k, n in c.get("statuses", {}).items() if k.startswith("stale:")
                    ),
                }
            )
    return cells


def summarize_cells(cells: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(cells)
    if not n:
        return {"cells": 0}
    changed = [c for c in cells if c["changed"]]
    steady = [c for c in cells if c["has_prev"] and not c["changed"]]
    by_key = {(c["world"], c["checkpoint"], c["qid"]): c for c in cells}
    preserved = kept = 0
    for c in steady:
        before = by_key.get((c["world"], previous_checkpoint(c["checkpoint"]), c["qid"]))
        if before is not None and before["correct"]:
            kept += 1
            preserved += int(c["correct"])
    explicit = [c for c in changed if c["explicit"]]
    implicit = [c for c in changed if c["explicit"] is False]
    value_change = [c for c in changed if c["value_change"]]
    unanswerable = [c for c in cells if c["label"] == UNANSWERABLE]
    answerable = [c for c in cells if c["label"] != UNANSWERABLE]
    return {
        "cells": n,
        "accuracy": rate(sum(c["correct"] for c in cells), n),
        "accuracy_answerable": rate(sum(c["correct"] for c in answerable), len(answerable)),
        "accuracy_unanswerable": rate(sum(c["correct"] for c in unanswerable), len(unanswerable)),
        "abstain_answerable": rate(sum(c["abstained"] for c in answerable), len(answerable)),
        "witnessed_accuracy": rate(sum(c["witnessed"] for c in cells), n),
        "witness_f1": statistics.fmean(c["witness_f1"] for c in cells),
        "support_recall": statistics.fmean(c["support_recall"] for c in cells),
        "changed_cells": len(changed),
        "refresh": rate(sum(c["correct"] for c in changed), len(changed)),
        "refresh_explicit": rate(sum(c["correct"] for c in explicit), len(explicit)),
        "refresh_implicit": rate(sum(c["correct"] for c in implicit), len(implicit)),
        "value_change_cells": len(value_change),
        "refresh_value_change": rate(sum(c["correct"] for c in value_change), len(value_change)),
        "old_value_rate": rate(sum(c["old_value"] for c in value_change), len(value_change)),
        "stale_abstention_rate": rate(sum(c["stale_abstention"] for c in changed), len(changed)),
        "preservation": rate(preserved, kept),
        "query_tokens_mean": statistics.fmean(c["tokens"] for c in cells),
        "stale_pages_served_per_query": statistics.fmean(c["stale_served"] for c in cells),
        "by_family": {
            fam: rate(
                sum(c["correct"] for c in cells if c["family"] == fam),
                sum(c["family"] == fam for c in cells),
            )
            for fam in sorted({c["family"] for c in cells})
        },
        "by_checkpoint": {
            ck: rate(
                sum(c["correct"] for c in cells if c["checkpoint"] == ck),
                sum(c["checkpoint"] == ck for c in cells),
            )
            for ck in sorted({c["checkpoint"] for c in cells}, key=lambda k: int(k[1:]))
        },
    }


def previous_checkpoint(cid: str) -> str:
    pct = int(cid[1:])
    return f"p{pct - 20}"


# ---------------------------------------------------------------------------- report


def build_report(worlds: list[World], answerers: list[str], systems: list[SystemSpec]) -> dict:
    rep: dict[str, Any] = {
        "worlds": [w.name for w in worlds],
        "model": MODEL,
        "answerers": answerers,
        "systems": {s.name: s.note for s in systems},
        "settings": {
            "fractions": FRACTIONS,
            "context_chars": CONTEXT_CHARS,
            "read_fraction": READ_FRACTION,
            "lazy_budget": LAZY_BUDGET,
            "periodic_every": PERIODIC_EVERY,
            "budget_fractions": BUDGET_FRACTIONS,
            "page_min_facts": PAGE_MIN_FACTS,
        },
        "by_answerer": {},
        "cost": {},
        "equivalence": {},
        "comparisons": {},
    }
    extraction = {w.name: read_json(OUT / "factlogs" / f"{w.name}.json") for w in worlds}
    rep["extraction"] = {
        "tokens": sum(x["tokens"] for x in extraction.values()),
        "calls": sum(x["calls"] for x in extraction.values()),
        "failed_sessions": sum(len(x["failed"]) for x in extraction.values()),
        "facts": sum(len(f) for x in extraction.values() for f in x["sessions"].values()),
    }
    sims = {
        (w.name, s.name): read_json(OUT / "contexts" / f"{w.name}__{s.name}.json")
        for w in worlds
        for s in systems
        if (OUT / "contexts" / f"{w.name}__{s.name}.json").exists()
    }
    for s in systems:
        rows = [sims[(w.name, s.name)] for w in worlds if (w.name, s.name) in sims]
        if not rows:
            continue
        rep["cost"][s.name] = {
            "worlds": len(rows),
            "maintenance_tokens": sum(r["maintenance"]["cost"] for r in rows),
            "wasted_tokens": sum(r["maintenance"]["wasted_cost"] for r in rows),
            "page_builds": sum(r["maintenance"]["jobs"] for r in rows),
            "page_calls": sum(r["page_calls"] for r in rows),
            "page_live_calls": sum(r["page_live_calls"] for r in rows),
            "pages_defined": sum(r["pages_defined"] for r in rows),
            "db_bytes_mean": statistics.fmean(r["db_bytes"] for r in rows),
            "assert_failures": sum(r["assert_failures"] for r in rows),
            "dirty_pages_at_checkpoints": statistics.fmean(
                sum(n for st, n in t["nodes"].items() if st != "valid")
                for r in rows
                for t in r["trace"]
            )
            if rows[0]["trace"]
            else 0.0,
        }
        # C1: incremental pages equal the from-scratch rebuild at every checkpoint.
    for w in worlds:
        a, b = sims.get((w.name, "eager")), sims.get((w.name, "full_rebuild"))
        if a is None or b is None:
            continue
        same_pages = sum(a["page_hashes"][c] == b["page_hashes"][c] for c in a["page_hashes"])
        same_ctx = total_ctx = 0
        for cid, by_q in a["contexts"].items():
            for qid, c in by_q.items():
                total_ctx += 1
                same_ctx += int(c["rendered"] == b["contexts"][cid][qid]["rendered"])
        rep["equivalence"][w.name] = {
            "checkpoints_identical_pages": same_pages,
            "checkpoints": len(a["page_hashes"]),
            "contexts_identical": same_ctx,
            "contexts": total_ctx,
            "eager_tokens": a["maintenance"]["cost"],
            "rebuild_tokens": b["maintenance"]["cost"],
        }
    for ans in answerers:
        block: dict[str, Any] = {}
        cells_by_system: dict[str, list[dict[str, Any]]] = {}
        for s in systems:
            if not s.answered:
                continue
            cells = []
            for w in worlds:
                path = OUT / "answers" / f"{w.name}__{s.name}__{slug(ans)}.json"
                if path.exists() and (w.name, s.name) in sims:
                    cells += score_cells(w, s, read_json(path))
            if cells:
                cells_by_system[s.name] = cells
                block[s.name] = summarize_cells(cells)
        rep["by_answerer"][ans] = block
        rep["comparisons"][ans] = comparisons(cells_by_system)
    return rep


COMPARE = (
    ("eager", "bm25"),
    ("eager", "facts"),
    ("eager", "full_context"),
    ("eager", "untracked"),
    ("eager", "periodic"),
    ("lazy", "eager"),
    ("priority_15", "stale_priority_15"),
    ("priority_15", "facts"),
    ("eager", "priority_15"),
    ("eager", "priority_40"),
    ("demand_15", "priority_15"),
    ("demand_15", "fifo_15"),
    ("priority_15", "fifo_15"),
    ("demand_40", "priority_40"),
    ("demand_40", "fifo_40"),
    ("priority_40", "fifo_40"),
    ("priority_40", "periodic"),
    ("priority_40", "untracked"),
    ("facts", "bm25"),
)


def paired(
    ka: dict[Any, dict[str, Any]],
    kb: dict[Any, dict[str, Any]],
    keys: list[Any],
    metric: str = "correct",
) -> dict[str, Any] | None:
    """Paired accuracy difference of two systems over the given (world, checkpoint, qid) cells.

    The CI resamples questions (world, qid) with all their checkpoints. ``worlds_agree`` counts
    the worlds whose own difference has the same sign as the overall one.
    """
    if not keys:
        return None
    rows = [(k[0], k[2], int(ka[k][metric]), int(kb[k][metric])) for k in keys]
    diff = sum(a - b for _, _, a, b in rows) / len(rows)
    per_world: dict[str, int] = {}
    for w, _, a, b in rows:
        per_world[w] = per_world.get(w, 0) + a - b
    sign = (diff > 0) - (diff < 0)
    return {
        "cells": len(keys),
        "diff": diff,
        "ci95": cluster_bootstrap([((w, q), a, b) for w, q, a, b in rows]),
        "worlds_agree": sum(((d > 0) - (d < 0)) == sign for d in per_world.values()),
        "worlds": len(per_world),
    }


def comparisons(cells_by_system: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    out = {}
    for a, b in COMPARE:
        if a not in cells_by_system or b not in cells_by_system:
            continue
        ka = {(c["world"], c["checkpoint"], c["qid"]): c for c in cells_by_system[a]}
        kb = {(c["world"], c["checkpoint"], c["qid"]): c for c in cells_by_system[b]}
        keys = sorted(ka.keys() & kb.keys())
        if not keys:
            continue
        out[f"{a} vs {b}"] = {
            "all": paired(ka, kb, keys),
            "subsets": {
                name: paired(ka, kb, [k for k in keys if test(ka[k], kb[k])])
                for name, test in SUBSETS.items()
            },
            "witnessed": paired(ka, kb, keys, metric="witnessed"),
        }
    return out


# Post-hoc from the tripops_w1 dev check: the headline is accuracy over all cells. The
# label split shows abstention effects; stale_shown keeps cells where either system was served a
# stale page, the only cells where serving or withholding one can matter.
SUBSETS: dict[str, Callable[[dict[str, Any], dict[str, Any]], bool]] = {
    "changed": lambda x, _: x["changed"],
    "value_change": lambda x, _: x["value_change"],
    "answerable": lambda x, _: x["label"] != UNANSWERABLE,
    "unanswerable": lambda x, _: x["label"] == UNANSWERABLE,
    "stale_shown": lambda x, y: x["stale_served"] > 0 or y["stale_served"] > 0,
}


def pct(r: dict[str, Any] | None) -> str:
    if not r or not r.get("n"):
        return "-"
    return f"{100 * r['rate']:.1f}"


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [
        "# MemDerive maintenance benchmark",
        "",
        f"Worlds: {', '.join(rep['worlds'])}. Extraction and pages: `{rep['model']}`. "
        f"Checkpoints at {', '.join(f'{round(100 * f)}%' for f in rep['settings']['fractions'])} "
        "of sessions; gold recompiled by the dataset oracle at each; deterministic scoring.",
        "",
        f"Extraction: {rep['extraction']['calls']} calls, {rep['extraction']['tokens']:,} tokens, "
        f"{rep['extraction']['facts']:,} facts, {rep['extraction']['failed_sessions']} sessions "
        "failed. Tokens are counted locally (o200k_base) over prompt and completion.",
        "",
    ]
    for ans, block in rep["by_answerer"].items():
        if not block:
            continue
        lines += [
            f"## Answers by `{ans}`",
            "",
            "| system | cells | accuracy | answerable | unanswerable | abstains (answerable) | "
            "p100 | witnessed | "
            "refresh (changed) | explicit | implicit | value changes | old-value answers | "
            "stale abstentions | preservation | support recall | maint. tokens | query tokens |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for name, s in block.items():
            cost = rep["cost"].get(name, {})
            lines.append(
                f"| {name} | {s['cells']} | {pct(s['accuracy'])} | "
                f"{pct(s['accuracy_answerable'])} | {pct(s['accuracy_unanswerable'])} | "
                f"{pct(s.get('abstain_answerable'))} | {pct(s['by_checkpoint'].get('p100'))} | "
                f"{pct(s['witnessed_accuracy'])} | "
                f"{pct(s['refresh'])} | {pct(s['refresh_explicit'])} | "
                f"{pct(s['refresh_implicit'])} | {pct(s['refresh_value_change'])} | "
                f"{pct(s['old_value_rate'])} | {pct(s['stale_abstention_rate'])} | "
                f"{pct(s['preservation'])} | {100 * s['support_recall']:.1f} | "
                f"{cost.get('maintenance_tokens', 0):,.0f} | {s['query_tokens_mean']:,.0f} |"
            )
        first = next(iter(block.values()))
        lines += [
            "",
            "Answerable counts `answerable` and `closed_world_zero` gold; unanswerable is "
            "`unanswerable_incomplete`. Value changes are changed cells whose gold is a value "
            f"both before and after ({first['value_change_cells']} cells); old-value answers "
            "are the share of them answered with the previous value. Stale abstentions are "
            "changed cells answered `unanswerable_incomplete` as the previous gold was. "
            "Abstains (answerable) is the share of answerable cells answered "
            "`unanswerable_incomplete`. Witnessed counts a cell only if the answer is correct and "
            "its cited event ids match the gold support. The label split, value-change, "
            "stale-shown and witnessed comparisons and the abstention column were added after the "
            "tripops_w1 dev check; accuracy over all cells is the pre-registered headline.",
            "",
            "Paired differences (accuracy points; 95% CI from a bootstrap over the questions of "
            "these worlds, each drawn with all its checkpoints, so it does not cover variation "
            "across worlds; n = cells; worlds k/m = k of m worlds have a difference with the same "
            "sign as the overall one, whatever that sign is):",
            "",
        ]

        def fmt(p: dict[str, Any]) -> str:
            lo, hi = p["ci95"]
            return (
                f"{100 * p['diff']:+.1f} [{100 * lo:+.1f}, {100 * hi:+.1f}] "
                f"(n={p['cells']}, worlds {p['worlds_agree']}/{p['worlds']})"
            )

        for name, c in rep["comparisons"].get(ans, {}).items():
            parts = [f"{k} {fmt(p)}" for k, p in c["subsets"].items() if p]
            if c.get("witnessed"):
                parts.append(f"witnessed {fmt(c['witnessed'])}")
            lines.append(f"- {name}: {fmt(c['all'])}; " + "; ".join(parts))
        lines.append("")
    lines += [
        "## Maintenance cost",
        "",
        "| system | maint. tokens | wasted | page builds | pages | dirty pages at checkpoints | "
        "DB bytes |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, c in rep["cost"].items():
        lines.append(
            f"| {name} | {c['maintenance_tokens']:,.0f} | {c['wasted_tokens']:,.0f} | "
            f"{c['page_builds']} | {c['pages_defined']} | "
            f"{c['dirty_pages_at_checkpoints']:.1f} | {c['db_bytes_mean']:,.0f} |"
        )
    lines += ["", "## Incremental vs full rebuild", ""]
    for world, e in rep["equivalence"].items():
        saving = 1 - e["eager_tokens"] / e["rebuild_tokens"] if e["rebuild_tokens"] else 0.0
        lines.append(
            f"- {world}: identical pages at {e['checkpoints_identical_pages']}/{e['checkpoints']} "
            f"checkpoints, identical contexts {e['contexts_identical']}/{e['contexts']}; "
            f"eager {e['eager_tokens']:,.0f} vs rebuild {e['rebuild_tokens']:,.0f} tokens "
            f"({100 * saving:.1f}% saved)"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- main


def eager_costs(world: World) -> tuple[float, float]:
    """Eager's mean maintenance cost per session and per page job."""
    sim = read_json(OUT / "contexts" / f"{world.name}__eager.json")
    cost = sim["maintenance"]["cost"]
    return cost / len(world.sessions), cost / max(1, sim["maintenance"]["jobs"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["gold", "extract", "simulate", "answer", "report", "all"])
    ap.add_argument("--worlds", nargs="+", default=list(HELDOUT_WORLDS))
    ap.add_argument("--systems", nargs="+", default=[s.name for s in SYSTEMS])
    ap.add_argument("--answerers", nargs="+", default=[ANSWERERS[0]])
    ap.add_argument(
        "--secondary-systems",
        nargs="+",
        default=None,
        help="systems answered by answerers after the first (default: all)",
    )
    ap.add_argument("--tag", default="run")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--max-calls", type=int, default=32, help="concurrent model calls")
    args = ap.parse_args()
    global CALL_GATE
    CALL_GATE = threading.Semaphore(args.max_calls)
    if compile_gold is None:
        print(f"DeriveMem not found at {DERIVEMEM}; see benchmarks/README.md", file=sys.stderr)
        return 2
    for name in ("gold", "factlogs", "contexts", "answers"):
        (OUT / name).mkdir(parents=True, exist_ok=True)
    systems = [SPECS[n] for n in args.systems]
    worlds = [load_world(w) for w in args.worlds]
    store = ResponseStore(OUT / "responses.sqlite")
    model = make_backend(MODEL, store)
    steps = (
        ["gold", "extract", "simulate", "answer", "report"]
        if args.command == "all"
        else [args.command]
    )
    errors: list[str] = []
    if "extract" in steps:
        _, errs = run_pool("extract", len(worlds), worlds, lambda w: extract_world(w, model))
        errors += errs
    if "simulate" in steps:
        factlogs = {w.name: extract_world(w, model) for w in worlds}
        first = [s for s in systems if s.name == "eager"]
        rest = [s for s in systems if s.name != "eager"]
        for batch in (first, rest):
            jobs = [(w, s) for w in worlds for s in batch]
            needs_eager = any(s.schedule == "budget" for s in batch)
            costs = {w.name: eager_costs(w) for w in worlds} if needs_eager else {}

            def run_sim(job: tuple[World, SystemSpec], costs: dict = costs) -> None:
                w, s = job
                if s.schedule == "budget":
                    # An unbuilt page is declared at eager's mean page cost, so priority and
                    # demand order it like a built page of typical cost.
                    per_session, per_job = costs[w.name]
                    budget = (s.budget_fraction or 0.0) * per_session
                    simulate(w, s, factlogs[w.name], model, budget, per_job)
                else:
                    simulate(w, s, factlogs[w.name], model, 0.0)
                print(f"[simulate] {w.name}/{s.name} done", file=sys.stderr, flush=True)

            _, errs = run_pool("simulate", max(1, len(jobs)), jobs, run_sim)
            errors += errs
    if "answer" in steps:
        for i, ans in enumerate(args.answerers):
            backend = make_backend(ans, store)
            chosen = (
                systems
                if i == 0 or args.secondary_systems is None
                else [SPECS[n] for n in args.secondary_systems]
            )
            jobs = [(w, s) for w in worlds for s in chosen if s.answered]
            errors += answer_all(jobs, backend, args.workers)
    if "report" in steps:
        rep = build_report(worlds, args.answerers, systems)
        rep["errors"] = errors[:50]
        RESULTS.mkdir(exist_ok=True)
        write_json(RESULTS / f"memderive_{args.tag}.json", rep)
        (RESULTS / f"memderive_{args.tag}.md").write_text(render_markdown(rep))
        print(render_markdown(rep))
    if errors:
        print(f"{len(errors)} errors, e.g. {errors[0]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
