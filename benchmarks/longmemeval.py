#!/usr/bin/env python3
"""Run Strata and baselines on LongMemEval-S (cleaned) with real models.

Stages (all resumable; model responses are cached, per-question artifacts are reused):

  extract   LLM fact extraction over each question's haystack, session by session in date order.
            One fact log per question, shared by every Strata variant.
  retrieve  Replay the haystack into a fresh Strata database per system and retrieve context for
            the question. Records retrieval latency, fallback/suppression counts and maintenance.
  answer    Answer every (question, system) with each answer model using the *same* prompt and
            context caps, then judge with the official LongMemEval judge prompts.
  report    Aggregate into benchmarks/results/<tag>.{json,md}.

Example:
  python benchmarks/longmemeval.py run --n 30 --tag pilot \
      --answerers openai:gpt-4o-mini cursor:composer-2.5-fast cursor:grok-4.7-low-fast
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lme_models import BackendError, CachedBackend, ResponseStore, make_backend

from strata import Budget, Context, Event, FactWrite, Memory, Policy
from strata.errors import ExtractionError, StrataError
from strata.index import flatten
from strata.llm import ModelRequest, parse_extraction
from strata.profiles import define_profile
from strata.types import MaintenanceReport, RecallItem

HERE = Path(__file__).resolve().parent
DATA_PATH = HERE / "data" / "longmemeval_s_cleaned.json"
OUT = HERE / "out" / "longmemeval"
RESULTS = HERE / "results"

# Defaults are models reachable through the Cursor agent CLI; the OpenAI API key available when this
# was written had no credit, so `openai:<model>` specs are supported but were not used.
EXTRACTION_MODEL = "cursor:composer-2.5-fast"
JUDGE_MODEL = "cursor:cursor-grok-4.6-low-fast"  # primary judge
JUDGE2_MODEL = "cursor:composer-2.5-fast"  # secondary judge, used for agreement/robustness
GROUP_SESSIONS = 8  # sessions per extraction call
GROUP_CHARS = 30000

# Context conditions, identical for every system and answer model. raw_search gets the top
# CONTEXT_ITEMS events; Strata systems get up to CONTEXT_ITEMS current items (facts, profiles,
# timelines) followed by up to CONTEXT_ITEMS events (sources of those facts interleaved with
# event search hits). Every system's context is cut at the same CONTEXT_CHARS.
CONTEXT_ITEMS = 10
ITEM_CHARS = 3000
CONTEXT_CHARS = 24000
TIMELINE_KIND = "timeline"
# Strata maintenance configuration (fixed before running; see README "Benchmark").
BACKGROUND_BUDGET = 1.0  # cost units per ingested session for fifo/priority variants
LAZY_BUDGET = 8.0  # cost units per query for the lazy variant
SLOT_LIST_LIMIT = 120
SESSION_TEXT_LIMIT = 16000


@dataclass(frozen=True)
class SystemSpec:
    name: str
    facts: bool
    bg_budget: float | None = None  # None: no background maintenance
    policy: Policy = Policy.PRIORITY
    lazy_budget: float | None = None
    include_stale: bool = False  # stale_control serves withheld nodes as if they were current


SYSTEMS: tuple[SystemSpec, ...] = (
    SystemSpec("raw_search", facts=False),
    SystemSpec("strata_eager", facts=True, bg_budget=math.inf),
    SystemSpec("strata_priority", facts=True, bg_budget=BACKGROUND_BUDGET),
    SystemSpec("strata_lazy", facts=True, lazy_budget=LAZY_BUDGET),
    SystemSpec(
        "stale_control",
        facts=True,
        bg_budget=BACKGROUND_BUDGET,
        policy=Policy.FIFO,
        include_stale=True,
    ),
)
SYSTEM_NAMES = tuple(s.name for s in SYSTEMS)

# ---------------------------------------------------------------------------- data


@dataclass
class Session:
    sid: str
    date: str
    rounds: list[tuple[str, str, bool]]  # (event key, text, has_answer)
    user_text: str


def parse_date(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y/%m/%d (%a) %H:%M")


def load_questions(n: int, seed: int, extra_ku: int = 0) -> list[dict[str, Any]]:
    data = json.loads(DATA_PATH.read_text())
    rng = random.Random(seed)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for q in data:
        groups[q["question_type"]].append(q)
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for qtype in sorted(groups):
        items = groups[qtype]
        rng.shuffle(items)
        ranked.extend(((i + 0.5) / len(items), q["question_id"], q) for i, q in enumerate(items))
    ranked.sort(key=lambda r: (r[0], r[1]))  # stratified; the first n are a prefix of the first m>n
    questions = [r[2] for r in ranked]
    if n <= 0:
        return questions
    chosen = questions[:n]
    # Optional oversampling of knowledge-update questions (the update/staleness metric); the
    # report always shows per-type numbers and a task-averaged score because this skews the mix.
    extra = [q for q in questions[n:] if q["question_type"] == "knowledge-update"][:extra_ku]
    return chosen + extra


def prepare_sessions(q: dict[str, Any]) -> list[Session]:
    order = sorted(
        range(len(q["haystack_sessions"])), key=lambda i: (parse_date(q["haystack_dates"][i]), i)
    )
    sessions = []
    for pos, i in enumerate(order):
        date = q["haystack_dates"][i]
        rounds: list[dict[str, Any]] = []
        for turn in q["haystack_sessions"][i]:
            if turn["role"] == "user" or not rounds:
                rounds.append({"user": "", "assistant": [], "ha": False})
                if turn["role"] == "user":
                    rounds[-1]["user"] = turn["content"]
                    rounds[-1]["ha"] = bool(turn.get("has_answer"))
                    continue
            rounds[-1]["assistant"].append(turn["content"])
            rounds[-1]["ha"] = rounds[-1]["ha"] or bool(turn.get("has_answer"))
        built = []
        for j, r in enumerate(rounds):
            text = f"[{date}] user: {r['user']}"
            if r["assistant"]:
                text += "\nassistant: " + "\n".join(r["assistant"])
            built.append((f"s{pos}#{j}", text, r["ha"]))
        user_text = "\n".join(
            f"user: {t['content']}" for t in q["haystack_sessions"][i] if t["role"] == "user"
        )[:SESSION_TEXT_LIMIT]
        sessions.append(Session(q["haystack_session_ids"][i], date, built, user_text))
    return sessions


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}{threading.get_ident()}")
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------- extraction

EXTRACT_SYSTEM = (
    "You extract durable personal facts about the USER from chat sessions (the user's messages "
    "only) so they can be stored in a versioned fact memory. You are given several sessions in "
    "date order. Reply with a single JSON object only, no prose: "
    '{"sessions": {"<session number>": {"facts": [{"subject": str, "attribute": str, '
    '"value": <string, number, boolean or list; never null>, "evidence": <exact quote copied '
    'verbatim from that session\'s user messages>, "confidence": <number 0..1>}]}}}. '
    'Include every session number; use {"facts": []} when a session has none.\n'
    'Rules: subject is "user" for facts about the user, otherwise the lowercase name or role of '
    'the other person or entity (e.g. "sister"). attribute is a short snake_case name for a '
    "property whose value can change over time (e.g. current_job_title, "
    "personal_best_5k_time, korean_restaurants_tried_count). If an attribute listed under "
    "EXISTING SLOTS means the same thing, reuse its exact name; a newly stated value then "
    "replaces the old one. Within one session give each attribute at most once. Include only "
    "things the user says about their own life: possessions, plans, preferences, counts, dates, "
    "events, relationships, numbers. Skip generic questions, requests for help, hypotheticals, "
    "and anything the assistant said. Values must be self-contained (include units and dates). "
    "Copy evidence exactly, character for character."
)
_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.DOTALL)


def extract_prompt(group: list[tuple[int, Session]], slots: list[str]) -> str:
    listed = ", ".join(slots[-SLOT_LIST_LIMIT:]) if slots else "none"
    blocks = [
        f"=== SESSION {number} (date {s.date}) ===\n{s.user_text}" for number, s in group
    ]
    return f"EXISTING SLOTS (subject.attribute): {listed}\n\n" + "\n\n".join(blocks)


_SESSIONS_OBJECT = re.compile(r'\{\s*"sessions"')


def reply_object(text: str) -> Any:
    """The reply's JSON object. Models behind the agent harness sometimes add a stray line
    before it or a closing tag after it; the object itself must still be well formed, and every
    fact is then validated and grounded by ``parse_extraction``."""
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        start = _SESSIONS_OBJECT.search(body)
        if start is None:
            raise
        return json.JSONDecoder().raw_decode(body, start.start())[0]


def parse_group(text: str, group: list[tuple[int, Session]]) -> dict[int, Any]:
    """Per session: an ExtractionResult, or the error string for that session."""
    try:
        data = reply_object(text)
        sessions = data["sessions"]
        if not isinstance(sessions, dict):
            raise TypeError("sessions must be an object")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ExtractionError(f"reply is not the requested JSON object: {exc}") from exc
    out: dict[int, Any] = {}
    for number, session in group:
        sub = sessions.get(str(number))
        if not isinstance(sub, dict):
            out[number] = "session missing from reply"
            continue
        try:
            out[number] = parse_extraction(
                json.dumps(sub), source_text=session.user_text, require_evidence=True
            )
        except ExtractionError as exc:
            out[number] = str(exc)[:200]
    return out


def make_groups(sessions: list[Session]) -> list[list[tuple[int, Session]]]:
    groups: list[list[tuple[int, Session]]] = []
    chars = 0
    for number, s in enumerate(sessions):
        if not s.user_text.strip():
            continue
        if not groups or len(groups[-1]) >= GROUP_SESSIONS or chars + len(s.user_text) > GROUP_CHARS:
            groups.append([])
            chars = 0
        groups[-1].append((number, s))
        chars += len(s.user_text)
    return groups


def extract_question(q: dict[str, Any], sessions: list[Session], model: CachedBackend) -> dict:
    path = OUT / "factlogs" / f"{q['question_id']}.json"
    if path.exists():
        return read_json(path)
    slots: dict[str, None] = {}
    entries: list[dict[str, Any]] = [
        {"sid": s.sid, "facts": [], "failed": False, "in": 0, "out": 0, "calls": 0}
        for s in sessions
    ]
    for group in make_groups(sessions):
        results: dict[int, Any] = {}
        pending = group
        error = ""
        for _attempt in range(2):
            prompt = extract_prompt(pending, list(slots))
            if error:
                prompt += (
                    f"\n\nYour previous reply was rejected ({error}). "
                    "Reply again with corrected JSON only."
                )
            rec = model.call(ModelRequest(prompt, EXTRACT_SYSTEM, temperature=0.0, max_tokens=4000))
            first = entries[pending[0][0]]
            first["in"] += rec.in_tokens
            first["out"] += rec.out_tokens
            first["calls"] += 1
            try:
                parsed = parse_group(rec.text, pending)
            except ExtractionError as exc:
                error = str(exc)[:200]
                continue
            results.update({n: r for n, r in parsed.items() if not isinstance(r, str)})
            failed = [(n, s) for n, s in pending if isinstance(parsed[n], str)]
            if not failed:
                pending = []
                break
            error = "; ".join(f"session {n}: {parsed[n]}" for n, _ in failed)[:300]
            pending = failed
        for number, _ in pending:
            entries[number]["failed"] = True
            entries[number]["error"] = error
        for number, _ in group:
            result = results.get(number)
            if result is None:
                continue
            for f in result.facts:
                entries[number]["facts"].append(
                    {
                        "subject": f.subject,
                        "attribute": f.attribute,
                        "value": f.value,
                        "evidence": f.evidence,
                    }
                )
                key = f"{f.subject}.{f.attribute}"
                slots.pop(key, None)
                slots[key] = None
    log = {"qid": q["question_id"], "sessions": entries}
    write_json(path, log)
    return log


# ---------------------------------------------------------------------------- retrieval


def normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def locate_source(session: Session, evidence: str | None) -> str:
    if evidence:
        needle = normalise(evidence)
        for key, text, _ in session.rounds:
            if needle in normalise(text):
                return key
    return session.rounds[0][0]


def event_date(m: Memory, key: str) -> str | None:
    try:
        return m.event(key).payload.get("date")
    except StrataError:
        return None


def to_context_item(m: Memory, item: RecallItem) -> tuple[dict[str, str | None], list[str]]:
    events = [p.removeprefix("event:") for p in item.provenance if p.startswith("event:")]
    if item.kind == "event":
        payload = item.value if isinstance(item.value, dict) else {}
        return {"date": payload.get("date"), "text": str(payload.get("text", item.text))}, events
    if item.kind == "fact":
        return {"date": event_date(m, events[0]) if events else None, "text": item.text}, events
    return {"date": None, "text": f"{item.kind} of {item.key}: {item.text}"}, events


def render_context(items: list[dict[str, str | None]]) -> str:
    parts, used = [], 0
    for i, it in enumerate(items, 1):
        text = (it["text"] or "")[:ITEM_CHARS]
        header = f"Memory item {i}" + (f" (dated {it['date']})" if it["date"] else "")
        block = f"{header}:\n{text}"
        if used + len(block) > CONTEXT_CHARS:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) if parts else "(no memory items retrieved)"


def make_timeline(subject: str, attribute: str) -> Callable[[Context], dict[str, Any]]:
    def timeline(ctx: Context) -> dict[str, Any]:
        changes = [
            {
                "value": c["value"] if c["op"] == "assert" else "(retracted)",
                "date": ctx.event(c["source"]).get("date"),
            }
            for c in ctx.history(subject, attribute)
        ]
        return {
            "fact": f"{subject}.{attribute}",
            "current": changes[-1]["value"],
            "current_since": changes[-1]["date"],
            "earlier": changes[:-1],
        }

    return timeline


def timeline_search_text(value: Any) -> str:
    return flatten([value["current"], [e["value"] for e in value["earlier"]]])


def define_timelines(m: Memory, writes: list[FactWrite], defined: set[tuple[str, str]]) -> None:
    """A timeline node per fact whose value has changed at least once."""
    for w in writes:
        slot = (w.subject, w.attribute)
        if slot in defined:
            continue
        values = {json.dumps(f.value, sort_keys=True) for f in m.fact_history(*slot)}
        if len(values) >= 2:
            m.define(
                TIMELINE_KIND,
                f"{w.subject}/{w.attribute}",
                1,
                make_timeline(*slot),
                search_text=timeline_search_text,
            )
            defined.add(slot)


def run_system(
    q: dict[str, Any], sessions: list[Session], factlog: dict, spec: SystemSpec
) -> dict[str, Any]:
    evidence_keys = {key for s in sessions for key, _, ha in s.rounds if ha}
    maintenance = MaintenanceReport()
    asserted = assert_failures = 0
    with tempfile.TemporaryDirectory() as tmp, Memory.open(str(Path(tmp) / "m.db")) as m:
        defined: set[str] = set()
        timelines: set[tuple[str, str]] = set()
        started = time.perf_counter()
        for s, entry in zip(sessions, factlog["sessions"], strict=True):
            m.ingest(
                s.sid,
                [Event(key, s.sid, "message", {"text": text, "date": s.date})
                 for key, text, _ in s.rounds],
            )
            if spec.facts and entry["facts"]:
                writes = [
                    FactWrite(f["subject"], f["attribute"], f["value"],
                              locate_source(s, f["evidence"]))
                    for f in entry["facts"]
                ]
                try:
                    m.assert_facts(writes)
                    asserted += len(writes)
                    for w in writes:
                        if w.subject not in defined:
                            define_profile(m, w.subject)
                            defined.add(w.subject)
                    define_timelines(m, writes, timelines)
                except StrataError:
                    assert_failures += 1
            if spec.bg_budget is not None:
                budget = Budget.unlimited() if math.isinf(spec.bg_budget) else Budget(spec.bg_budget)
                maintenance.merge(m.maintain(budget, spec.policy))
        ingest_s = time.perf_counter() - started
        dirty_before_query = sum(
            n for state, n in m.status()["nodes"].items() if state != "valid"
        )

        started = time.perf_counter()
        if spec.name == "raw_search":
            recalled = m.search_events(q["question"], CONTEXT_ITEMS)
            fallback_used, suppressed, refreshed, warnings = True, 0, 0, ()
        else:
            res = m.recall(
                q["question"],
                limit=CONTEXT_ITEMS,
                include_stale=spec.include_stale,
                lazy_budget=spec.lazy_budget,
                events=CONTEXT_ITEMS,
            )
            recalled = list(res.items)
            fallback_used, suppressed, refreshed, warnings = (
                res.fallback_used, res.suppressed, res.refreshed, res.warnings)
        latency_ms = (time.perf_counter() - started) * 1000

        context, seen_events, statuses = [], set(), Counter()
        for item in recalled:
            ctx, events = to_context_item(m, item)
            context.append(ctx)
            seen_events.update(events)
            statuses[f"{item.status}:{item.kind if item.kind in ('event', 'fact') else 'node'}"] += 1
        return {
            "context": context,
            "rendered": render_context(context),
            "retrieval_ms": latency_ms,
            "fallback_used": fallback_used,
            "suppressed": suppressed,
            "refreshed": refreshed,
            "warnings": list(warnings),
            "statuses": dict(statuses),
            "stale_items_served": sum(v for k, v in statuses.items() if k.startswith("stale:")),
            "evidence_hit": (bool(seen_events & evidence_keys) if evidence_keys else None),
            "maintenance": {k: v for k, v in maintenance.as_dict().items() if k != "outcomes"},
            "dirty_nodes_at_query": dirty_before_query,
            "facts_asserted": asserted,
            "assert_failures": assert_failures,
            "timelines": len(timelines),
            "ingest_s": ingest_s,
            "node_states": m.status()["nodes"],
        }


def retrieve_question(q: dict[str, Any], model: CachedBackend) -> dict:
    path = OUT / "contexts" / f"{q['question_id']}.json"
    if path.exists():
        return read_json(path)
    sessions = prepare_sessions(q)
    factlog = extract_question(q, sessions, model)
    out = {
        "qid": q["question_id"],
        "n_sessions": len(sessions),
        "extraction": {
            "in": sum(e["in"] for e in factlog["sessions"]),
            "out": sum(e["out"] for e in factlog["sessions"]),
            "calls": sum(e.get("calls", 0) for e in factlog["sessions"]),
            "failed_sessions": sum(e["failed"] for e in factlog["sessions"]),
            "facts": sum(len(e["facts"]) for e in factlog["sessions"]),
        },
        "systems": {s.name: run_system(q, sessions, factlog, s) for s in SYSTEMS},
    }
    write_json(path, out)
    return out


# ---------------------------------------------------------------------------- answering

ANSWER_SYSTEM = (
    "You are a personal assistant with a memory of past conversations with the user. Answer the "
    "question using only the memory items provided. Memory items are retrieved snippets and may "
    "be incomplete; each may carry a date. If the memory shows the same thing with different "
    "values at different times, the most recent one is the current one. If the memory does not "
    "contain the information needed to answer, reply starting with 'ABSTAIN:' followed by a "
    "brief explanation of what is missing. Otherwise answer concisely."
)


def answer_request(q: dict[str, Any], rendered: str) -> ModelRequest:
    prompt = (
        f"Current date: {q['question_date']}\n\nMemory:\n{rendered}\n\n"
        f"Question: {q['question']}\nAnswer:"
    )
    return ModelRequest(prompt, ANSWER_SYSTEM, temperature=0.0, max_tokens=400)


def is_abstention(text: str) -> bool:
    return text.strip().upper().startswith("ABSTAIN")


_JUDGE_DEFAULT = (
    "I will give you a question, a correct answer, and a response from a model. Please answer "
    "yes if the response contains the correct answer. Otherwise, answer no. If the response is "
    "equivalent to the correct answer or contains all the intermediate steps to get the correct "
    "answer, you should also answer yes. If the response only contains a subset of the "
    "information required by the answer, answer no. \n\nQuestion: {q}\n\nCorrect Answer: {a}\n\n"
    "Model Response: {r}\n\nIs the model response correct? Answer yes or no only."
)
_JUDGE_TEMPORAL = (
    "I will give you a question, a correct answer, and a response from a model. Please answer "
    "yes if the response contains the correct answer. Otherwise, answer no. If the response is "
    "equivalent to the correct answer or contains all the intermediate steps to get the correct "
    "answer, you should also answer yes. If the response only contains a subset of the "
    "information required by the answer, answer no. In addition, do not penalize off-by-one "
    "errors for the number of days. If the question asks for the number of days/weeks/months, "
    "etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is "
    "18), the model's response is still correct. \n\nQuestion: {q}\n\nCorrect Answer: {a}\n\n"
    "Model Response: {r}\n\nIs the model response correct? Answer yes or no only."
)
_JUDGE_UPDATE = (
    "I will give you a question, a correct answer, and a response from a model. Please answer "
    "yes if the response contains the correct answer. Otherwise, answer no. If the response "
    "contains some previous information along with an updated answer, the response should be "
    "considered as correct as long as the updated answer is the required answer.\n\n"
    "Question: {q}\n\nCorrect Answer: {a}\n\nModel Response: {r}\n\n"
    "Is the model response correct? Answer yes or no only."
)
_JUDGE_PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, and a response from "
    "a model. Please answer yes if the response satisfies the desired response. Otherwise, "
    "answer no. The model does not need to reflect all the points in the rubric. The response is "
    "correct as long as it recalls and utilizes the user's personal information correctly.\n\n"
    "Question: {q}\n\nRubric: {a}\n\nModel Response: {r}\n\n"
    "Is the model response correct? Answer yes or no only."
)
_JUDGE_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response from a model. "
    "Please answer yes if the model correctly identifies the question as unanswerable. The model "
    "could say that the information is incomplete, or some other information is given but the "
    "asked information is not.\n\nQuestion: {q}\n\nExplanation: {a}\n\nModel Response: {r}\n\n"
    "Does the model correctly identify the question as unanswerable? Answer yes or no only."
)

STALE_LABEL_PROMPT = (
    "A user's situation changed over time. Below is the evidence from the user's earlier and later "
    "messages in chronological order, the question, the correct CURRENT answer, and a model's "
    "response. Classify the response with exactly one label:\n"
    "current_only: states the correct current answer and does not present an outdated value as "
    "current.\n"
    "current_with_outdated_mention: states the correct current answer but also mentions an "
    "outdated earlier value.\n"
    "outdated_as_current: gives an earlier, since-superseded value as the answer.\n"
    "abstained: declines or says the information is missing.\n"
    "other_wrong: wrong for some other reason.\n\n"
    "Evidence (chronological):\n{evidence}\n\nQuestion: {q}\n\nCorrect current answer: {a}\n\n"
    "Model response: {r}\n\nReply with only the label."
)
STALE_LABELS = (
    "current_only",
    "current_with_outdated_mention",
    "outdated_as_current",
    "abstained",
    "other_wrong",
)


def judge_prompt(q: dict[str, Any], response: str) -> str:
    qtype = q["question_type"]
    if q["question_id"].endswith("_abs"):
        template = _JUDGE_ABSTENTION
    elif qtype == "temporal-reasoning":
        template = _JUDGE_TEMPORAL
    elif qtype == "knowledge-update":
        template = _JUDGE_UPDATE
    elif qtype == "single-session-preference":
        template = _JUDGE_PREFERENCE
    else:
        template = _JUDGE_DEFAULT
    return template.format(q=q["question"], a=q["answer"], r=response)


def update_evidence(q: dict[str, Any]) -> str:
    rows = []
    for sid, date, session in zip(
        q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"], strict=True
    ):
        if sid in q["answer_session_ids"]:
            for turn in session:
                if turn.get("has_answer"):
                    rows.append((parse_date(date), f"[{date}] {turn['content'][:700]}"))
    rows.sort(key=lambda r: r[0])
    return "\n".join(r[1] for r in rows)


def slug(spec: str) -> str:
    return spec.replace(":", "_").replace("/", "_")


def answer_task(
    q: dict[str, Any],
    system: str,
    context: dict,
    answerer: CachedBackend,
    judge: CachedBackend,
    judge2: CachedBackend,
    gates: dict[str, threading.Semaphore],
) -> dict:
    path = OUT / "answers" / f"{q['question_id']}__{system}__{slug(answerer.name)}.json"
    if path.exists():
        return read_json(path)
    with gates[answerer.name]:
        rec = answerer.call(answer_request(q, context["systems"][system]["rendered"]))
    response = rec.text.strip()
    verdicts = []
    for j in (judge, judge2):
        with gates[j.name]:
            verdicts.append(
                j.call(ModelRequest(judge_prompt(q, response), temperature=0.0, max_tokens=20)).text
            )
    out: dict[str, Any] = {
        "qid": q["question_id"],
        "system": system,
        "answerer": answerer.name,
        "response": response,
        "abstained": is_abstention(response),
        "correct": verdicts[0].strip().lower().startswith("yes"),
        "correct_judge2": verdicts[1].strip().lower().startswith("yes"),
        "in_tokens": rec.in_tokens,
        "out_tokens": rec.out_tokens,
        "latency_s": rec.latency_s,
        "label": None,
    }
    if q["question_type"] == "knowledge-update" and not q["question_id"].endswith("_abs"):
        with gates[judge.name]:
            label = judge.call(
                ModelRequest(
                    STALE_LABEL_PROMPT.format(
                        evidence=update_evidence(q), q=q["question"], a=q["answer"], r=response
                    ),
                    temperature=0.0,
                    max_tokens=20,
                )
            ).text.strip().lower()
        out["label"] = next((lab for lab in STALE_LABELS if lab in label), "other_wrong")
    write_json(path, out)
    return out


# ---------------------------------------------------------------------------- reporting


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    z, p = 1.96, k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)


def rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n if n else None), "ci95": [lo, hi]}


def pctl(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))] if s else math.nan


def paired_bootstrap(a: list[int], b: list[int], seed: int = 0, rounds: int = 2000) -> list[float]:
    rng = random.Random(seed)
    n = len(a)
    diffs = []
    for _ in range(rounds):
        idx = [rng.randrange(n) for _ in range(n)]
        diffs.append(sum(a[i] - b[i] for i in idx) / n)
    diffs.sort()
    return [diffs[int(0.025 * rounds)], diffs[int(0.975 * rounds)]]


def build_report(
    questions: list[dict[str, Any]],
    contexts: dict[str, dict],
    answers: dict[tuple[str, str, str], dict],
    answerers: list[str],
    tag: str,
) -> dict[str, Any]:
    qmap = {q["question_id"]: q for q in questions}
    types = sorted({q["question_type"] for q in questions})
    report: dict[str, Any] = {
        "tag": tag,
        "dataset": "LongMemEval-S cleaned (xiaowu0162/longmemeval-cleaned, longmemeval_s_cleaned.json)",
        "dataset_sha256": hashlib.sha256(DATA_PATH.read_bytes()).hexdigest(),
        "n_questions": len(questions),
        "by_type_n": dict(Counter(q["question_type"] for q in questions)),
        "n_abstention_questions": sum(q["question_id"].endswith("_abs") for q in questions),
        "models": {
            "extraction": EXTRACTION_MODEL,
            "judge": JUDGE_MODEL,
            "judge2": JUDGE2_MODEL,
            "answerers": answerers,
        },
        "config": {
            "context_items": CONTEXT_ITEMS,
            "item_chars": ITEM_CHARS,
            "context_chars": CONTEXT_CHARS,
            "strata_recall_events": CONTEXT_ITEMS,
            "derived_nodes": "profile per subject, timeline per changed fact",
            "background_budget_per_session": BACKGROUND_BUDGET,
            "lazy_budget_per_query": LAZY_BUDGET,
        },
        "systems": {},
        "answerers": {},
    }
    ctx_qids = [qid for qid in qmap if qid in contexts]
    ext = [contexts[qid]["extraction"] for qid in ctx_qids]
    report["extraction"] = {
        "questions": len(ext),
        "calls": sum(e["calls"] for e in ext),
        "in_tokens": sum(e["in"] for e in ext),
        "out_tokens": sum(e["out"] for e in ext),
        "failed_sessions": sum(e["failed_sessions"] for e in ext),
        "sessions": sum(contexts[qid]["n_sessions"] for qid in ctx_qids),
        "facts": sum(e["facts"] for e in ext),
    }
    for system in SYSTEM_NAMES:
        rows = [contexts[qid]["systems"][system] for qid in ctx_qids]
        lat = [r["retrieval_ms"] for r in rows]
        ev = [r["evidence_hit"] for r in rows if r["evidence_hit"] is not None]
        maint = defaultdict(float)
        for r in rows:
            for k, v in r["maintenance"].items():
                maint[k] += v
        report["systems"][system] = {
            "questions": len(rows),
            "retrieval_ms": {
                "mean": statistics.fmean(lat),
                "p50": pctl(lat, 0.5),
                "p95": pctl(lat, 0.95),
            },
            "fallback_used": rate(sum(r["fallback_used"] for r in rows), len(rows)),
            "queries_with_withheld_nodes": rate(sum(r["suppressed"] > 0 for r in rows), len(rows)),
            "queries_with_dirty_nodes": rate(
                sum(r["dirty_nodes_at_query"] > 0 for r in rows), len(rows)
            ),
            "stale_items_served": sum(r["stale_items_served"] for r in rows),
            "queries_serving_stale": rate(sum(r["stale_items_served"] > 0 for r in rows), len(rows)),
            "evidence_in_context": rate(sum(ev), len(ev)),
            "lazy_refreshed_nodes": sum(r["refreshed"] for r in rows),
            "maintenance_totals": dict(maint),
            "maintenance_per_question": {k: v / len(rows) for k, v in maint.items()},
            "facts_asserted": sum(r["facts_asserted"] for r in rows),
            "assert_failures": sum(r["assert_failures"] for r in rows),
            "timelines_per_question": statistics.fmean(r.get("timelines", 0) for r in rows),
            "ingest_s_mean": statistics.fmean(r["ingest_s"] for r in rows),
            "context_chars_mean": statistics.fmean(len(r["rendered"]) for r in rows),
        }
    for ans in answerers:
        block: dict[str, Any] = {}
        raw_vec: dict[str, int] = {}
        for system in SYSTEM_NAMES:
            recs = [
                (qmap[qid], answers[(qid, system, ans)])
                for qid in qmap
                if (qid, system, ans) in answers
            ]
            if not recs:
                continue
            per_type = {}
            for t in types:
                sub = [r for q, r in recs if q["question_type"] == t]
                per_type[t] = rate(sum(r["correct"] for r in sub), len(sub))
            abs_recs = [r for q, r in recs if q["question_id"].endswith("_abs")]
            non_abs = [r for q, r in recs if not q["question_id"].endswith("_abs")]
            ku = [r for q, r in recs
                  if q["question_type"] == "knowledge-update" and not q["question_id"].endswith("_abs")]
            labels = Counter(r["label"] for r in ku)
            valid_types = [per_type[t]["rate"] for t in types if per_type[t]["n"]]
            vec = {r["qid"]: int(r["correct"]) for _, r in recs}
            if system == "raw_search":
                raw_vec = vec
            entry = {
                "n": len(recs),
                "accuracy": rate(sum(r["correct"] for _, r in recs), len(recs)),
                "task_averaged_accuracy": statistics.fmean(valid_types),
                "by_type": per_type,
                "accuracy_judge2": rate(sum(r["correct_judge2"] for _, r in recs), len(recs)),
                "judge_agreement": rate(
                    sum(r["correct"] == r["correct_judge2"] for _, r in recs), len(recs)
                ),
                "knowledge_update_accuracy": rate(sum(r["correct"] for r in ku), len(ku)),
                "knowledge_update_strict_current_only": rate(labels["current_only"], len(ku)),
                "knowledge_update_outdated_as_current": rate(labels["outdated_as_current"], len(ku)),
                "knowledge_update_labels": dict(labels),
                "abstention_questions_correct": rate(sum(r["correct"] for r in abs_recs), len(abs_recs)),
                "abstained_on_answerable": rate(sum(r["abstained"] for r in non_abs), len(non_abs)),
                "abstained_overall": rate(sum(r["abstained"] for _, r in recs), len(recs)),
                "answer_latency_s_mean": statistics.fmean(r["latency_s"] for _, r in recs),
                "answer_latency_s_p95": pctl([r["latency_s"] for _, r in recs], 0.95),
                "in_tokens_mean": statistics.fmean(r["in_tokens"] for _, r in recs),
                "out_tokens_mean": statistics.fmean(r["out_tokens"] for _, r in recs),
            }
            shared = [k for k in vec if k in raw_vec]
            if system != "raw_search" and shared:
                entry["paired_diff_vs_raw"] = {
                    "n": len(shared),
                    "mean": statistics.fmean(vec[k] - raw_vec[k] for k in shared),
                    "ci95": paired_bootstrap([vec[k] for k in shared], [raw_vec[k] for k in shared]),
                }
            block[system] = entry
        report["answerers"][ans] = block
    return report


def fmt_rate(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a"
    return f"{r['rate']:.3f} ({r['k']}/{r['n']})"


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [f"# LongMemEval-S results: {rep['tag']}", ""]
    lines += [
        f"Dataset: {rep['dataset']}, sha256 `{rep['dataset_sha256'][:16]}`.",
        f"Questions: {rep['n_questions']} ({rep['n_abstention_questions']} abstention). "
        f"By type: {rep['by_type_n']}.",
        f"Models: extraction `{rep['models']['extraction']}`, judge `{rep['models']['judge']}` "
        f"(secondary judge `{rep['models']['judge2']}`), "
        f"answerers {rep['models']['answerers']}.",
        f"Config: {rep['config']}.",
        f"Extraction: {rep['extraction']}.",
        "",
    ]
    for ans, block in rep["answerers"].items():
        lines += [f"## Answer model `{ans}`", ""]
        lines += [
            "| system | n | accuracy | task-avg | accuracy (judge2) | judge agreement | KU acc |"
            " KU strict current-only | KU outdated-as-current | abs-question acc |"
            " abstain on answerable | paired diff vs raw (95% CI) |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for system, e in block.items():
            d = e.get("paired_diff_vs_raw")
            diff = f"{d['mean']:+.3f} [{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}]" if d else "-"
            lines.append(
                f"| {system} | {e['n']} | {fmt_rate(e['accuracy'])} | {e['task_averaged_accuracy']:.3f} |"
                f" {fmt_rate(e['accuracy_judge2'])} | {fmt_rate(e['judge_agreement'])} |"
                f" {fmt_rate(e['knowledge_update_accuracy'])} |"
                f" {fmt_rate(e['knowledge_update_strict_current_only'])} |"
                f" {fmt_rate(e['knowledge_update_outdated_as_current'])} |"
                f" {fmt_rate(e['abstention_questions_correct'])} |"
                f" {fmt_rate(e['abstained_on_answerable'])} | {diff} |"
            )
        types = sorted(next(iter(block.values()))["by_type"]) if block else []
        lines += ["", "| system | " + " | ".join(types) + " |", "|---|" + "---|" * len(types)]
        for system, e in block.items():
            lines.append(
                f"| {system} | " + " | ".join(fmt_rate(e["by_type"][t]) for t in types) + " |"
            )
        lines += ["", "| system | answer latency mean/p95 (s) | prompt tokens | completion tokens |",
                  "|---|---|---|---|"]
        for system, e in block.items():
            lines.append(
                f"| {system} | {e['answer_latency_s_mean']:.2f} / {e['answer_latency_s_p95']:.2f} |"
                f" {e['in_tokens_mean']:.0f} | {e['out_tokens_mean']:.1f} |"
            )
        lines.append("")
    lines += [
        "## Retrieval and maintenance (answer-model independent)",
        "",
        "| system | retrieval ms mean/p50/p95 | fallback used | withheld nodes | dirty at query |"
        " stale items served | evidence in context | maint. jobs/q | maint. cost/q | wasted/q |"
        " lazy refreshed | ctx chars | timelines/q |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for system, s in rep["systems"].items():
        mp = s["maintenance_per_question"]
        r = s["retrieval_ms"]
        lines.append(
            f"| {system} | {r['mean']:.1f} / {r['p50']:.1f} / {r['p95']:.1f} |"
            f" {fmt_rate(s['fallback_used'])} | {fmt_rate(s['queries_with_withheld_nodes'])} |"
            f" {fmt_rate(s['queries_with_dirty_nodes'])} | {s['stale_items_served']} |"
            f" {fmt_rate(s['evidence_in_context'])} | {mp.get('jobs', 0):.1f} |"
            f" {mp.get('cost', 0):.1f} | {mp.get('wasted_cost', 0):.1f} |"
            f" {s['lazy_refreshed_nodes']} | {s['context_chars_mean']:.0f} |"
            f" {s['timelines_per_question']:.1f} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- driver


def run_pool(
    label: str, workers: int, items: Iterable[Any], fn: Callable[[Any], Any]
) -> tuple[list[Any], list[str]]:
    items = list(items)
    results, errors = [], []
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): item for item in items}
        for done, fut in enumerate(as_completed(futures), 1):
            try:
                results.append(fut.result())
            except BackendError as exc:
                errors.append(str(exc)[:300])
            if done % 25 == 0 or done == len(items):
                print(
                    f"[{label}] {done}/{len(items)} done, {len(errors)} errors, "
                    f"{time.time() - started:.0f}s",
                    file=sys.stderr,
                    flush=True,
                )
    return results, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["run", "report"])
    ap.add_argument("--n", type=int, default=0, help="questions (stratified prefix); 0 = all 500")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--extra-ku", type=int, default=0, help="extra knowledge-update questions")
    ap.add_argument("--answerers", nargs="+",
                    default=["cursor:composer-2.5-fast", "cursor:grok-4.7-low-fast"])
    ap.add_argument("--stages", nargs="+", default=["retrieve", "answer"],
                    choices=["retrieve", "answer"])
    ap.add_argument("--extract-workers", type=int, default=10)
    ap.add_argument("--openai-concurrency", type=int, default=16)
    ap.add_argument("--cursor-concurrency", type=int, default=10, help="per model")
    args = ap.parse_args()

    if not DATA_PATH.exists():
        print(f"missing dataset {DATA_PATH}; see benchmarks/README.md", file=sys.stderr)
        return 2
    questions = load_questions(args.n, args.seed, args.extra_ku)
    qmap = {q["question_id"]: q for q in questions}
    store = ResponseStore(OUT / "responses.sqlite")
    extractor = make_backend(EXTRACTION_MODEL, store)
    judge = make_backend(JUDGE_MODEL, store)
    judge2 = make_backend(JUDGE2_MODEL, store)
    answer_models = {spec: make_backend(spec, store) for spec in args.answerers}
    gates = {
        b.name: threading.Semaphore(
            args.cursor_concurrency if b.name.startswith("cursor:") else args.openai_concurrency
        )
        for b in {x.name: x for x in [extractor, judge, judge2, *answer_models.values()]}.values()
    }
    errors: list[str] = []

    if args.command == "run" and "retrieve" in args.stages:
        _, errs = run_pool(
            "retrieve", args.extract_workers, questions, lambda q: retrieve_question(q, extractor)
        )
        errors += errs
    contexts = {
        qid: read_json(OUT / "contexts" / f"{qid}.json")
        for qid in qmap
        if (OUT / "contexts" / f"{qid}.json").exists()
    }

    if args.command == "run" and "answer" in args.stages:
        tasks = [
            (qmap[qid], system, ans)
            for ans in answer_models
            for qid in contexts
            for system in SYSTEM_NAMES
        ]
        _, errs = run_pool(
            "answer",
            args.cursor_concurrency * (len(answer_models) + 2),
            tasks,
            lambda t: answer_task(t[0], t[1], contexts[t[0]["question_id"]],
                                  answer_models[t[2]], judge, judge2, gates),
        )
        errors += errs

    answers: dict[tuple[str, str, str], dict] = {}
    for path in (OUT / "answers").glob("*.json") if (OUT / "answers").exists() else []:
        rec = read_json(path)
        if rec["qid"] in qmap and rec["answerer"] in {m.name for m in answer_models.values()}:
            answers[(rec["qid"], rec["system"], rec["answerer"])] = rec
    report = build_report(
        [qmap[qid] for qid in contexts], contexts, answers,
        [m.name for m in answer_models.values()], args.tag,
    )
    report["backend_errors"] = errors
    report["live_calls"] = {
        b.name: {"live": b.live_calls, "cache_hits": b.cache_hits}
        for b in [extractor, judge, judge2, *answer_models.values()]
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"longmemeval_{args.tag}.json").write_text(json.dumps(report, indent=2))
    (RESULTS / f"longmemeval_{args.tag}.md").write_text(render_markdown(report))
    print(render_markdown(report))
    if errors:
        print(f"{len(errors)} backend errors (first: {errors[0]})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
