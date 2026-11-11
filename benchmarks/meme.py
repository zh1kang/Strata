"""MEME benchmark: dependency-aware memory against flat stores, BM25 and full context.

MEME (filler32k split, 100 episodes) states facts about a user, some of them tied to
another fact ("this is determined by my X; if X changes this would likely change too")
or given a successor ("if X changes, my Y will be Z"). Later X changes. Questions then
check that the dependent fact is updated (Cas), reported as uncertain when no successor
was given (Abs), deleted on request (Del), recalled verbatim (ER), listed as a history
(Tr) or aggregated (Agg).

One extraction pass per episode (user turns, one call per session) feeds three stores:

* ``facts_plain``: a plain fact store; dependencies and successor rules are dropped.
* ``facts_annotated``: the same facts with their dependencies and the successor rules
  rendered as text, so the answering model has to work out what changed.
* ``strata``: dependencies are fact premises and successor rules are contingent writes,
  so the engine applies successors and flags facts whose premise changed as unverified.

Baselines: ``full_context`` (the whole transcript, MEME's in-context row) and ``bm25``
(Strata's FTS5 BM25 over session chunks, top 5 as in MEME's BM25 agent). Answers use
MEME's unified answer prompt; MEME's judge prompts score them, including its
trivial-pass rule (a Cas/Abs/Del pass only counts if the same entity's before-question
passed).

Usage (from benchmarks/):
  PYTHONPATH=../src uv run --extra bench python meme.py all --episodes 10
  PYTHONPATH=../src uv run --extra bench python meme.py all
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import tempfile
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from lme_models import CachedBackend, ResponseStore, make_backend, request_tokens
from longmemeval import cluster_bootstrap, rate, read_json, run_pool, write_json
from memderive import json_object

from strata import Event, FactWrite, Memory, StrataError
from strata.llm import ModelRequest

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "meme_filler32k.json"
OUT = HERE / "out" / "meme"
RESULTS = HERE / "results"

EXTRACTOR = "cursor:composer-2.5-fast"  # existing factlogs were built with it; keep for cache
ANSWERERS = ("cursor:composer-2.5", "cursor:grok-4.7-low")
JUDGES = ("cursor:grok-4.7-low", "cursor:composer-2.5")
SYSTEMS = ("full_context", "bm25", "facts_plain", "facts_annotated", "strata")
FACT_SYSTEMS = ("facts_plain", "facts_annotated", "strata")
USER = "user"
BM25_TOP_K = 5
CHUNK_CHARS = 16000  # about MEME's 4096-token session chunks
CALL_GATE = threading.Semaphore(32)
LOCK = threading.Lock()

UNIFIED_ANSWER_PROMPT = (
    "Answer the user's question based ONLY on the context provided below.\n"
    "If the information is not in the context, say you don't have that information.\n"
    "Answer with ONLY the value. Do not explain or add context.\n\n"
    "Context:\n{context}\n\nQuestion: {question}"
)

EXTRACT_SYSTEM = (
    "You maintain a structured memory about one user. From the user's messages in one "
    "conversation session, extract every durable fact the user states about themselves, their "
    "life or their work: identity, relationships, health, possessions, routines, preferences, "
    "plans, projects, tools, settings and so on. Ignore questions, small talk and general "
    "knowledge.\n\n"
    "Each fact is an attribute of the user with a short snake_case name. When a fact is about "
    "the same thing as an attribute in KNOWN FACTS, reuse that name (a new car replaces "
    "vehicle). Values are short strings; keep a value verbatim when the user asks for it to be "
    "remembered word for word.\n"
    'op: "set" for a new or replacing value; "retract" when the user says it no longer applies '
    'or asks you to forget or remove it (value null); "add" or "remove" for an attribute '
    "that holds several items at once.\n"
    'depends_on: attribute names this fact was stated to be determined by or tied to ("this is '
    'determined by my X; if X changes this would likely change too"). Empty if none.\n'
    "rules: when the user says what an attribute WILL become if another attribute changes "
    '("if my X changes, my Y will be Z"), add {"attribute": Y, "value": Z, "if_changed": [X]}. '
    "Do not record Z as a current fact.\n\n"
    'Reply with JSON only: {"facts": [{"attribute": ..., "value": ..., "op": ..., '
    '"depends_on": [...]}], "rules": [{"attribute": ..., "value": ..., "if_changed": [...]}]}'
)

HEADERS = {
    "facts_plain": "Facts the user has stated (current value first, then earlier values):",
    "facts_annotated": (
        "Facts the user has stated (current value first, then earlier values). Some facts were "
        "stated as depending on another fact, with that fact's value at the time; the user's "
        "rules say what a fact becomes when another fact changes."
    ),
    "strata": (
        "Facts the user has stated (current value first, then earlier values). A fact marked "
        "UNVERIFIED was based on another fact that has since changed, so it may no longer hold."
    ),
}


# ---------------------------------------------------------------------------- data


def load_episodes(limit: int | None) -> list[dict[str, Any]]:
    episodes = read_json(DATA)
    return episodes[:limit] if limit else episodes


def iso(ts: str) -> str:
    return datetime.strptime(ts, "%Y/%m/%d (%a) %H:%M").isoformat()


def positions(ep: dict[str, Any]) -> dict[str, int]:
    """Sessions ingested before each question phase."""
    return {
        "before": ep["before_questions"]["position_after_session"] + 1,
        "after": ep["after_questions"]["position_after_session"] + 1,
    }


def questions(ep: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    return ep[f"{phase}_questions"]["questions"]


def user_turns(session: dict[str, Any]) -> list[str]:
    return [t["content"] for t in session["conversation"] if t.get("role") == "user"]


def transcript(sessions: list[dict[str, Any]]) -> str:
    """MEME's in-context transcript format (eval/in_context_baseline.py)."""
    parts = []
    for i, s in enumerate(sessions):
        parts.append(f"[Session: {s.get('timestamp', f'session_{i}')}]")
        for turn in s.get("conversation", []):
            role = "User" if turn.get("role") == "user" else "Assistant"
            parts.append(f"{role}: {turn.get('content', '')}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------- extraction


def known_facts(m: Memory) -> str:
    facts = m.facts(USER)
    if not facts:
        return "(none yet)"
    return "\n".join(f"- {f.attribute}: {json.dumps(f.value)}" for f in facts)


def parse_extraction(text: str) -> dict[str, list[dict[str, Any]]]:
    data = json_object(text)
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    facts, rules = [], []
    for f in data.get("facts") or []:
        if not isinstance(f, dict) or not isinstance(f.get("attribute"), str):
            continue
        op = f.get("op", "set")
        if op not in ("set", "add", "remove", "retract"):
            op = "set"
        if op != "retract" and f.get("value") in (None, "", []):
            continue
        deps = [d for d in f.get("depends_on") or [] if isinstance(d, str) and d]
        facts.append(
            {
                "attribute": normalize(f["attribute"]),
                "value": f.get("value"),
                "op": op,
                "depends_on": sorted({normalize(d) for d in deps} - {normalize(f["attribute"])}),
            }
        )
    for r in data.get("rules") or []:
        if not isinstance(r, dict) or not isinstance(r.get("attribute"), str):
            continue
        deps = [d for d in r.get("if_changed") or [] if isinstance(d, str) and d]
        deps = sorted({normalize(d) for d in deps} - {normalize(r["attribute"])})
        if deps and r.get("value") not in (None, "", []):
            rules.append(
                {"attribute": normalize(r["attribute"]), "value": r["value"], "if_changed": deps}
            )
    return {"facts": facts, "rules": rules}


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "unnamed"


def plain_writes(extracted: dict[str, Any], source: str) -> list[FactWrite]:
    return [FactWrite(USER, f["attribute"], f["value"], source, mode=f["op"]) for f in extracted]


def extract_episode(ep: dict[str, Any], model: CachedBackend) -> dict[str, Any]:
    """Facts per session, each call seeing the facts known so far (from a plain store)."""
    path = OUT / "factlogs" / f"{ep['episode_id']}.json"
    if path.exists():
        return read_json(path)
    ledger: Counter[str] = Counter()
    sessions, failed = [], []
    with tempfile.TemporaryDirectory() as tmp, Memory.open(str(Path(tmp) / "x.db")) as m:
        for i, s in enumerate(ep["sessions"][: positions(ep)["after"]]):
            key = f"s{i:02d}"
            turns = user_turns(s)
            m.ingest(
                key, [Event(key, key, "session", {"text": "\n".join(turns)}, iso(s["timestamp"]))]
            )
            prompt = (
                f"KNOWN FACTS:\n{known_facts(m)}\n\nSESSION ({s['timestamp']}), the user's "
                "messages:\n" + "\n".join(f"- {t}" for t in turns)
            )
            result, error = None, ""
            for _attempt in range(3):
                request = ModelRequest(
                    prompt
                    + (
                        f"\n\nYour previous reply was rejected ({error}). Reply with JSON only."
                        if error
                        else ""
                    ),
                    EXTRACT_SYSTEM,
                    max_tokens=4000,
                )
                with CALL_GATE:
                    rec = model.call(request)
                ledger["calls"] += 1
                ledger["tokens"] += request_tokens(request, rec.text)
                try:
                    result = parse_extraction(rec.text)
                    break
                except (ValueError, json.JSONDecodeError) as exc:
                    error = str(exc)[:200]
            if result is None:
                failed.append(key)
                result = {"facts": [], "rules": []}
            for w in plain_writes(result["facts"], key):
                with contextlib.suppress(StrataError):
                    m.assert_facts([w])
            sessions.append(result)
    out = {"sessions": sessions, "failed": failed, **ledger}
    write_json(path, out)
    return out


# ---------------------------------------------------------------------------- stores


def strata_writes(extracted: dict[str, Any], source: str, mode: str) -> list[FactWrite]:
    writes = []
    for f in extracted["facts"]:
        premises = (
            tuple((USER, d) for d in f["depends_on"])
            if mode != "facts_plain" and f["op"] != "retract"
            else ()
        )
        writes.append(
            FactWrite(USER, f["attribute"], f["value"], source, mode=f["op"], premises=premises)
        )
    if mode == "strata":
        for r in extracted["rules"]:
            writes.append(
                FactWrite(
                    USER,
                    r["attribute"],
                    r["value"],
                    source,
                    mode="contingent",
                    premises=tuple((USER, d) for d in r["if_changed"]),
                )
            )
    return writes


def apply(m: Memory, writes: list[FactWrite]) -> int:
    """Assert as one batch; on a rejected batch, write one by one. Returns rejected writes."""
    try:
        m.assert_facts(writes)
        return 0
    except StrataError:
        rejected = 0
        for w in writes:
            try:
                m.assert_facts([w])
            except StrataError:
                rejected += 1
        return rejected


def show(value: Any) -> str:
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def day(at: str | None) -> str:
    return at[:10] if at else "?"


def head_premises(m: Memory, attribute: str) -> list[tuple[str, Any]]:
    rows = m.store.db.execute(
        "SELECT fp.attribute, fp.value FROM slots s JOIN slot_heads h ON h.slot_id = s.slot_id"
        " JOIN fact_premises fp ON fp.version_id = h.version_id"
        " WHERE s.subject=? AND s.attribute=? ORDER BY fp.attribute",
        (USER, attribute),
    ).fetchall()
    return [(r[0], json.loads(r[1]) if r[1] is not None else None) for r in rows]


def render_facts(m: Memory, mode: str, rules: list[tuple[str, dict[str, Any]]]) -> str:
    lines = [HEADERS[mode]]
    for f in m.facts(USER):
        earlier = [
            h
            for h in m.fact_history(USER, f.attribute)
            if h.op == "assert" and h.version_id != f.version_id and h.value != f.value
        ]
        line = f"- {f.attribute}: {show(f.value)} (since {day(f.at)})"
        if earlier:
            seen: list[str] = []
            for h in earlier:
                text = f"{show(h.value)} (from {day(h.at)})"
                if text not in seen:
                    seen.append(text)
            line += "; earlier: " + "; ".join(seen)
        if mode == "facts_annotated":
            deps = head_premises(m, f.attribute)
            if deps:
                line += "; depends on " + ", ".join(
                    f"{a} (which was {show(v) if v is not None else 'unknown'} then)"
                    for a, v in deps
                )
        if mode == "strata":
            status = m.fact_status(USER, f.attribute)
            if status.status != "current":
                line += f" [UNVERIFIED: {'; '.join(status.reasons)}]"
        lines.append(line)
    if mode == "facts_annotated" and rules:
        lines.append("Rules the user stated:")
        for at, r in rules:
            lines.append(
                f"- if {' or '.join(r['if_changed'])} changes, {r['attribute']} becomes "
                f"{show(r['value'])} (stated {day(at)})"
            )
    return "\n".join(lines)


def build_contexts(ep: dict[str, Any], factlog: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Context per system and phase, built by replaying the sessions."""
    pos = positions(ep)
    sessions = ep["sessions"]
    out: dict[str, dict[str, str]] = {s: {} for s in SYSTEMS}
    with tempfile.TemporaryDirectory() as tmp:
        stores = {
            mode: Memory.open(str(Path(tmp) / f"{mode}.db")) for mode in (*FACT_SYSTEMS, "bm25")
        }
        rules: list[tuple[str, dict[str, Any]]] = []
        rejected = 0
        try:
            for i in range(pos["after"]):
                s, key, at = sessions[i], f"s{i:02d}", iso(sessions[i]["timestamp"])
                text = "\n".join(user_turns(s))
                for mode in FACT_SYSTEMS:
                    stores[mode].ingest(key, [Event(key, key, "session", {"text": text}, at)])
                    rejected += apply(
                        stores[mode], strata_writes(factlog["sessions"][i], key, mode)
                    )
                rules += [(at, r) for r in factlog["sessions"][i]["rules"]]
                chunk = transcript([s])
                stores["bm25"].ingest(
                    key,
                    [
                        Event(f"{key}:{j}", key, "chunk", {"text": chunk[o : o + CHUNK_CHARS]}, at)
                        for j, o in enumerate(range(0, len(chunk), CHUNK_CHARS))
                    ],
                )
                for phase, n in pos.items():
                    if i + 1 != n:
                        continue
                    out["full_context"][phase] = transcript(sessions[:n])
                    for mode in FACT_SYSTEMS:
                        out[mode][phase] = render_facts(stores[mode], mode, rules)
                    out["bm25"][phase] = {
                        q["question"]: "\n\n---\n\n".join(
                            str(item.value.get("text", ""))
                            if isinstance(item.value, dict)
                            else item.text
                            for item in stores["bm25"].search_events(q["question"], BM25_TOP_K)
                        )
                        or "(no memory)"
                        for q in questions(ep, phase)
                    }
            out["_meta"] = {
                "rejected_writes": rejected,
                "edges": edge_recovery(ep, stores["strata"], factlog),
            }
        finally:
            for store in stores.values():
                store.close()
    return out


def edge_recovery(ep: dict[str, Any], m: Memory, factlog: dict[str, Any]) -> dict[str, Any]:
    """Did extraction record each gold dependency edge as a premise or a contingency?"""
    db = m.store.db
    slots = {}
    for (attribute,) in db.execute("SELECT attribute FROM slots WHERE subject=?", (USER,)):
        history = m.fact_history(USER, attribute)
        slots[attribute] = [show(h.value).lower() for h in history if h.op == "assert"]
    links = {
        (source, target)
        for target, source in db.execute(
            "SELECT s.attribute, fp.attribute FROM slots s"
            " JOIN fact_versions v ON v.slot_id = s.slot_id"
            " JOIN fact_premises fp ON fp.version_id = v.version_id WHERE s.subject=?",
            (USER,),
        )
    }
    links |= {
        (d, r["attribute"])
        for s in factlog["sessions"]
        for r in s["rules"]
        for d in r["if_changed"]
    }

    def slot_of(entity: str) -> str | None:
        value = ep["entities"].get(entity, {}).get("before")
        if not isinstance(value, str):
            return None
        v = value.lower()
        for attribute, values in slots.items():
            if any(v == x or v in x for x in values):
                return attribute
        return None

    found = []
    for edge in ep["dependency_edges_used"]:
        src, dst = slot_of(edge["source"]), slot_of(edge["target"])
        found.append(bool(src and dst and (src, dst) in links))
    return {"edges": len(found), "recovered": sum(found)}


# ---------------------------------------------------------------------------- answering


def context_for(contexts: dict[str, Any], system: str, phase: str, question: str) -> str:
    value = contexts[system][phase]
    return value[question] if isinstance(value, dict) else value


def answer_all(
    episodes: list[dict[str, Any]], systems: list[str], answerer: CachedBackend, workers: int
) -> list[str]:
    tasks, files = [], {}
    for system in systems:
        path = OUT / "answers" / f"{system}__{slug(answerer.name)}.json"
        done: dict[str, Any] = read_json(path) if path.exists() else {}
        files[system] = (path, done)
        for ep in episodes:
            contexts = read_json(OUT / "contexts" / f"{ep['episode_id']}.json")
            for phase in ("before", "after"):
                for i, q in enumerate(questions(ep, phase)):
                    key = f"{ep['episode_id']}/{phase}/{i}"
                    if key not in done:
                        ctx = context_for(contexts, system, phase, q["question"])
                        tasks.append((key, ctx, q["question"], done))

    def run(task: tuple[str, str, str, dict[str, Any]]) -> None:
        key, ctx, question, done = task
        request = ModelRequest(
            UNIFIED_ANSWER_PROMPT.format(context=ctx, question=question), max_tokens=500
        )
        with CALL_GATE:
            rec = answerer.call(request)
        with LOCK:
            done[key] = {"answer": rec.text.strip(), "tokens": request_tokens(request, rec.text)}

    _, errors = run_pool(f"answer {answerer.name}", workers, tasks, run)
    for path, done in files.values():
        write_json(path, done)
    return errors


def slug(spec: str) -> str:
    return spec.replace(":", "_").replace("/", "_")


# ---------------------------------------------------------------------------- judging
# Prompts verbatim from MEME's code/eval/judge.py.

BEFORE_COMMON_PROMPT = """You are a judge evaluating whether an AI agent's answer is semantically correct.

QUESTION: {question}
GOLD: {gold}
AGENT: {agent_answer}

Does the agent's answer contain the correct information matching the gold answer?

Rules:
- Focus on semantic equivalence, not exact wording
- If the gold value is present in the agent's answer, it is correct — regardless of any additional information, future possibilities, or extra details the agent mentions
- "Dentist every 6 months; dermatologist monthly (if you change residence)" → gold is "dentist (every 6 months)" → YES (core answer correct, extra info is irrelevant)
- "40 minutes" = "40 min" → YES
- "dentist appointment every 6 months" = "dentist (every 6 months)" → YES
- If agent says "I don't know" and gold is a specific value → NO

Output ONLY JSON: {{"correct": true/false, "reason": "one sentence"}}"""  # noqa: E501

CASCADE_D_PROMPT = """You are a judge evaluating whether an AI agent correctly reports a CASCADE-DEPENDENT value after an upstream change.

QUESTION: {question}
GOLD (new cascaded value): {gold}
AGENT: {agent_answer}

The upstream entity changed, triggering a cascade. The agent should now report ONLY the new value.

Rules:
- Agent states the new value as the sole, definitive answer → YES
- Agent lists BOTH old and new values together → NO
- Agent states only the old value → NO
- Agent lists multiple options or hedges ("might", "considering") → NO
- Agent uses future tense ("will change to X", "will be X") instead of confirming as current → NO
- "I don't know" → NO

Output ONLY JSON: {{"correct": true/false, "reason": "one sentence"}}"""  # noqa: E501

CASCADE_U_PROMPT = """You are a judge evaluating whether an AI agent appropriately handles UNCERTAINTY after an upstream entity changed.

QUESTION: {question}
GOLD: {gold}
AGENT: {agent_answer}

The upstream entity changed, making the previous answer potentially invalid. The agent should express uncertainty.

Rules:
- Agent says "I don't know" / "not sure" / "Unknown" / "None" / expresses uncertainty → YES
- Agent acknowledges the upstream change and questions validity → YES
- Agent confidently states the OLD value as definitive current fact → NO
- Agent provides the old value with no hedging or uncertainty → NO

Output ONLY JSON: {{"correct": true/false, "reason": "one sentence"}}"""  # noqa: E501

DELETION_PROMPT = """You are a judge evaluating whether an AI agent correctly handles a DELETED piece of information.

QUESTION: {question}
GOLD: {gold}
AGENT: {agent_answer}

The information was explicitly deleted/removed. The agent should not return it.

Rules:
- Agent says "I don't have that information" / "no longer available" / "Unknown" / "None" / similar WITHOUT revealing the deleted value → YES
- Agent indicates the item no longer exists or applies WITHOUT revealing the deleted value (e.g., "No food allergies" when the deleted value was a specific allergy) → YES
- Agent reveals the deleted value in any way (even while saying it was deleted/removed) → NO
- Agent returns the deleted value as if it still exists → NO

Output ONLY JSON: {{"correct": true/false, "reason": "one sentence"}}"""  # noqa: E501

MULTIHOP_PROMPT = """You are a judge evaluating whether an AI agent's answer contains specific values.

QUESTION: {question}
TARGET VALUES: {gold_values}
AGENT: {agent_answer}

For EACH target value, check whether it appears (exact or paraphrased) in the agent's answer.

Rules:
- The value appears in the answer (exact or paraphrased) → true
- The value is absent → false
- "I don't know" or "I don't have that information" → all false

Output ONLY JSON: {{"results": [{{"value": "...", "present": true/false}}], "reason": "one sentence"}}"""  # noqa: E501

MULTIPLE_UPDATE_PROMPT = """You are a judge evaluating whether an AI agent correctly recalls a sequence of historical values for an entity that changed multiple times.

QUESTION: {question}
GOLD (ordered history values, comma-separated): {gold}
AGENT: {agent_answer}

The user's information changed multiple times. The question asks for the FULL HISTORY in chronological order (earliest to latest).

Rules:
- Agent's answer must contain ALL gold values in the EXACT order specified
- Order matters: the values must appear earliest-to-latest as in the gold
- Extra surrounding text is fine, but the gold sequence must be preserved
- Missing any value → NO
- Wrong order → NO
- Only some values → NO
- "I don't know" → NO

Output ONLY JSON: {{"correct": true/false, "reason": "one sentence"}}"""  # noqa: E501

JUDGE_PROMPTS = {
    "Tr": MULTIPLE_UPDATE_PROMPT,
    "Cas": CASCADE_D_PROMPT,
    "Abs": CASCADE_U_PROMPT,
    "Del": DELETION_PROMPT,
    "Agg": MULTIHOP_PROMPT,
}


def judge_request(q: dict[str, Any], answer: str, phase: str) -> ModelRequest | bool:
    """MEME's u_check / u_check_multi; ER is a deterministic substring match."""
    task = q["task_type"].split(" (")[0]
    values = q["entity_values"]
    if task == "ER":
        gold = " ".join(str(next(iter(values.values()))).lower().split())
        return gold in " ".join(answer.lower().split())
    if task == "Agg":
        gold_values = ", ".join(f'"{v}"' for v in values.values())
        prompt = MULTIHOP_PROMPT.format(
            question=q["question"], gold_values=gold_values, agent_answer=answer
        )
    elif task == "Tr":
        history = next(iter(values.values()))
        history = history if isinstance(history, list) else [history]
        prompt = MULTIPLE_UPDATE_PROMPT.format(
            question=q["question"], gold=", ".join(history), agent_answer=answer
        )
    else:
        template = BEFORE_COMMON_PROMPT if phase == "before" else JUDGE_PROMPTS[task]
        prompt = template.format(
            question=q["question"], gold=next(iter(values.values())), agent_answer=answer
        )
    return ModelRequest(prompt, max_tokens=300)


def judge_verdict(text: str, q: dict[str, Any]) -> bool:
    try:
        data = json_object(text)
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if q["task_type"].split(" (")[0] == "Agg":
        results = data.get("results") or []
        n = len(q["entity_values"])
        flags = [bool(r.get("present")) for r in results[:n] if isinstance(r, dict)]
        return len(flags) == n and all(flags)
    return data.get("correct") is True


def judge_all(
    episodes: list[dict[str, Any]],
    systems: list[str],
    answerer: str,
    judge: CachedBackend,
    workers: int,
) -> list[str]:
    tasks, files = [], {}
    for system in systems:
        answers_path = OUT / "answers" / f"{system}__{slug(answerer)}.json"
        if not answers_path.exists():
            continue
        answers = read_json(answers_path)
        path = OUT / "judgments" / f"{system}__{slug(answerer)}__{slug(judge.name)}.json"
        done: dict[str, Any] = read_json(path) if path.exists() else {}
        files[system] = (path, done)
        for ep in episodes:
            for phase in ("before", "after"):
                for i, q in enumerate(questions(ep, phase)):
                    key = f"{ep['episode_id']}/{phase}/{i}"
                    if key in answers and key not in done:
                        tasks.append((key, q, phase, answers[key]["answer"], done))

    def run(task: tuple[str, dict[str, Any], str, str, dict[str, Any]]) -> None:
        key, q, phase, answer, done = task
        request = judge_request(q, answer, phase)
        if isinstance(request, bool):
            verdict = request
        else:
            with CALL_GATE:
                rec = judge.call(request)
            verdict = judge_verdict(rec.text, q)
        with LOCK:
            done[key] = verdict

    _, errors = run_pool(f"judge {judge.name}", workers, tasks, run)
    for path, done in files.values():
        write_json(path, done)
    return errors


# ---------------------------------------------------------------------------- report


def score(episodes: list[dict[str, Any]], verdicts: dict[str, bool]) -> list[dict[str, Any]]:
    """After-question rows; Cas/Abs/Del pass only with a passing before-question (MEME rule)."""
    rows = []
    for ep in episodes:
        before: dict[str, bool] = {}
        for i, q in enumerate(questions(ep, "before")):
            v = verdicts.get(f"{ep['episode_id']}/before/{i}")
            if v is not None:
                before[next(iter(q["entity_values"]))] = v
        for i, q in enumerate(questions(ep, "after")):
            v = verdicts.get(f"{ep['episode_id']}/after/{i}")
            if v is None:
                continue
            task = q["task_type"].split(" (")[0]
            entity = next(iter(q["entity_values"]))
            knew = before.get(entity, False)
            rows.append(
                {
                    "key": f"{ep['episode_id']}/after/{i}",
                    "task": task,
                    "hop": q.get("hop"),
                    "raw": v,
                    "pass": v and (knew if task in ("Cas", "Abs", "Del") else True),
                }
            )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = sorted({r["task"] for r in rows})
    by_task = {
        t: {
            "pass": rate(
                sum(r["pass"] for r in rows if r["task"] == t), sum(r["task"] == t for r in rows)
            ),
            "raw": rate(
                sum(r["raw"] for r in rows if r["task"] == t), sum(r["task"] == t for r in rows)
            ),
        }
        for t in tasks
    }
    cas2 = [r for r in rows if r["task"] == "Cas" and r["hop"] == 2]
    return {
        "after": rate(sum(r["pass"] for r in rows), len(rows)),
        "task_mean": sum(by_task[t]["pass"]["rate"] for t in tasks) / len(tasks) if tasks else None,
        "by_task": by_task,
        "cas_2hop": rate(sum(r["pass"] for r in cas2), len(cas2)),
    }


COMPARE = (
    ("strata", "facts_plain"),
    ("strata", "facts_annotated"),
    ("strata", "bm25"),
    ("strata", "full_context"),
    ("facts_annotated", "facts_plain"),
)


def build_report(
    episodes: list[dict[str, Any]], answerers: list[str], judges: list[str]
) -> dict[str, Any]:
    logs = [read_json(OUT / "factlogs" / f"{ep['episode_id']}.json") for ep in episodes]
    metas = [read_json(OUT / "contexts" / f"{ep['episode_id']}.json")["_meta"] for ep in episodes]
    rep: dict[str, Any] = {
        "episodes": len(episodes),
        "extractor": EXTRACTOR,
        "extraction": {
            "calls": sum(lg.get("calls", 0) for lg in logs),
            "tokens": sum(lg.get("tokens", 0) for lg in logs),
            "failed_sessions": sum(len(lg["failed"]) for lg in logs),
            "facts": sum(len(s["facts"]) for lg in logs for s in lg["sessions"]),
            "rules": sum(len(s["rules"]) for lg in logs for s in lg["sessions"]),
            "with_premises": sum(
                bool(f["depends_on"]) for lg in logs for s in lg["sessions"] for f in s["facts"]
            ),
            "rejected_writes": sum(m["rejected_writes"] for m in metas),
        },
        "edge_recovery": rate(
            sum(m["edges"]["recovered"] for m in metas), sum(m["edges"]["edges"] for m in metas)
        ),
        "results": {},
        "judge_agreement": {},
    }
    for ans in answerers:
        for system in SYSTEMS:
            path = OUT / "answers" / f"{system}__{slug(ans)}.json"
            if not path.exists():
                continue
            answers = read_json(path)
            tokens = [a["tokens"] for a in answers.values()]
            for judge in judges:
                jpath = OUT / "judgments" / f"{system}__{slug(ans)}__{slug(judge)}.json"
                if not jpath.exists():
                    continue
                rows = score(episodes, read_json(jpath))
                entry = summarize(rows)
                entry["query_tokens_mean"] = sum(tokens) / len(tokens) if tokens else 0
                rep["results"].setdefault(ans, {}).setdefault(judge, {})[system] = entry
                rep["results"][ans][judge][system]["_rows"] = rows
        if len(judges) > 1:
            a, b = judges[0], judges[1]
            agree = total = 0
            for system in SYSTEMS:
                pa = OUT / "judgments" / f"{system}__{slug(ans)}__{slug(a)}.json"
                pb = OUT / "judgments" / f"{system}__{slug(ans)}__{slug(b)}.json"
                if pa.exists() and pb.exists():
                    va, vb = read_json(pa), read_json(pb)
                    for k in va.keys() & vb.keys():
                        total += 1
                        agree += va[k] == vb[k]
            rep["judge_agreement"][ans] = rate(agree, total)
    rep["comparisons"] = {}
    for ans, by_judge in rep["results"].items():
        for judge, block in by_judge.items():
            out = {}
            for x, y in COMPARE:
                if x not in block or y not in block:
                    continue
                kx = {r["key"]: r["pass"] for r in block[x]["_rows"]}
                ky = {r["key"]: r["pass"] for r in block[y]["_rows"]}
                keys = sorted(kx.keys() & ky.keys())
                a = [int(kx[k]) for k in keys]
                b = [int(ky[k]) for k in keys]
                out[f"{x} vs {y}"] = {
                    "questions": len(keys),
                    "diff": (sum(a) - sum(b)) / len(keys) if keys else None,
                    "ci95": cluster_bootstrap(
                        [(k.split("/")[0], x_, y_) for k, x_, y_ in zip(keys, a, b, strict=True)]
                    )
                    if keys
                    else None,
                }
            rep["comparisons"].setdefault(ans, {})[judge] = out
    for by_judge in rep["results"].values():
        for block in by_judge.values():
            for entry in block.values():
                entry.pop("_rows", None)
    return rep


def pct(r: dict[str, Any] | None) -> str:
    return "-" if not r or not r.get("n") else f"{100 * r['rate']:.1f}"


def render_markdown(rep: dict[str, Any]) -> str:
    ex = rep["extraction"]
    lines = [
        "# MEME (filler32k) results",
        "",
        f"{rep['episodes']} episodes. Extraction by `{rep['extractor']}`: {ex['calls']} calls, "
        f"{ex['tokens']:,} tokens, {ex['facts']} facts ({ex['with_premises']} with premises), "
        f"{ex['rules']} successor rules, {ex['failed_sessions']} failed sessions, "
        f"{ex['rejected_writes']} rejected writes. Gold dependency edges recorded: "
        f"{pct(rep['edge_recovery'])}% of {rep['edge_recovery']['n']}.",
        "",
        "Cas/Abs/Del count a pass only if the same entity's before-question passed (MEME's "
        "trivial-pass rule); raw judge pass rates in parentheses.",
        "",
    ]
    tasks = ("ER", "Agg", "Tr", "Del", "Cas", "Abs")
    for ans, by_judge in rep["results"].items():
        for judge, block in by_judge.items():
            lines += [
                f"## Answers by `{ans}`, judged by `{judge}`",
                "",
                "| system | after | task mean | "
                + " | ".join(tasks)
                + " | Cas 2-hop | query tokens |",
                "|---|---|---|" + "---|" * len(tasks) + "---|---|",
            ]
            for system, s in block.items():
                cells = []
                for t in tasks:
                    bt = s["by_task"].get(t)
                    if not bt:
                        cells.append("-")
                    elif t in ("Cas", "Abs", "Del"):
                        cells.append(f"{pct(bt['pass'])} ({pct(bt['raw'])})")
                    else:
                        cells.append(pct(bt["pass"]))
                mean = s["task_mean"]
                lines.append(
                    f"| {system} | {pct(s['after'])} | "
                    f"{'-' if mean is None else f'{100 * mean:.1f}'} | "
                    + " | ".join(cells)
                    + f" | {pct(s['cas_2hop'])} | {s['query_tokens_mean']:,.0f} |"
                )
            lines += [
                "",
                "Paired differences on after-questions "
                "(points, 95% CI from a bootstrap over episodes):",
                "",
            ]
            for name, c in rep["comparisons"].get(ans, {}).get(judge, {}).items():
                if c["diff"] is not None:
                    lines.append(
                        f"- {name}: {100 * c['diff']:+.1f} [{100 * c['ci95'][0]:+.1f}, "
                        f"{100 * c['ci95'][1]:+.1f}] over {c['questions']} questions"
                    )
            lines.append("")
    for ans, agreement in rep["judge_agreement"].items():
        lines.append(
            f"Judge agreement on `{ans}` answers: {pct(agreement)}% of {agreement['n']} verdicts."
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["extract", "contexts", "answer", "judge", "report", "all"])
    ap.add_argument("--episodes", type=int, default=0, help="first N episodes; 0 = all 100")
    ap.add_argument("--systems", nargs="+", default=list(SYSTEMS))
    ap.add_argument("--answerers", nargs="+", default=list(ANSWERERS))
    ap.add_argument("--judges", nargs="+", default=list(JUDGES))
    ap.add_argument("--tag", default="run")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--max-calls", type=int, default=32, help="concurrent model calls")
    args = ap.parse_args()
    global CALL_GATE
    CALL_GATE = threading.Semaphore(args.max_calls)
    for name in ("factlogs", "contexts", "answers", "judgments"):
        (OUT / name).mkdir(parents=True, exist_ok=True)
    episodes = load_episodes(args.episodes or None)
    store = ResponseStore(OUT / "responses.sqlite")
    steps = (
        ["extract", "contexts", "answer", "judge", "report"]
        if args.command == "all"
        else [args.command]
    )
    errors: list[str] = []
    if "extract" in steps:
        model = make_backend(EXTRACTOR, store)
        _, errs = run_pool(
            "extract", len(episodes), episodes, lambda ep: extract_episode(ep, model)
        )
        errors += errs
    if "contexts" in steps:

        def run_contexts(ep: dict[str, Any]) -> None:
            path = OUT / "contexts" / f"{ep['episode_id']}.json"
            if not path.exists():
                factlog = read_json(OUT / "factlogs" / f"{ep['episode_id']}.json")
                write_json(path, build_contexts(ep, factlog))

        run_pool("contexts", 8, episodes, run_contexts)
    if "answer" in steps:
        for ans in args.answerers:
            errors += answer_all(episodes, args.systems, make_backend(ans, store), args.workers)
    if "judge" in steps:
        for judge in args.judges:
            backend = make_backend(judge, store)
            for ans in args.answerers:
                errors += judge_all(episodes, args.systems, ans, backend, args.workers)
    if "report" in steps:
        rep = build_report(episodes, args.answerers, args.judges)
        rep["errors"] = errors[:50]
        RESULTS.mkdir(exist_ok=True)
        write_json(RESULTS / f"meme_{args.tag}.json", rep)
        (RESULTS / f"meme_{args.tag}.md").write_text(render_markdown(rep))
        print(render_markdown(rep))
    if errors:
        print(f"{len(errors)} errors, e.g. {errors[0]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
