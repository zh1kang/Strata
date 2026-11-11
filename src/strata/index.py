"""Incremental FTS5 index.

Index rows carry text only. Whether a hit is current, stale, or raw is always
resolved against the live tables at query time, never stored in the index.

Words are Porter-stemmed by the tokenizer and common English function words are
dropped from queries. An event's document also carries the facts extracted from
it, so a question phrased like a fact finds the conversation that stated it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

_TOKEN = re.compile(r"\w+", re.UNICODE)
_SEGMENT = re.compile(r"(?<=[.!?])\s+|\n+")
_SUFFIXES = ("ingly", "edly", "ing", "ies", "ed", "es", "ly", "s")

_STOPWORD_TEXT = (
    "a an the and or but if then so of in on at to for from by with about as into over after"
    " before is am are was were be been being do does did doing have has had having i me my"
    " mine we our you your he she it its they them their this that these those what which who"
    " whom whose when where why how many much long often can could should would will shall"
    " may might must not no yes any some all each every more most other than too very just"
    " also ever there here up down out off again s t d ll m re ve didn don doesn isn wasn"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def flatten(value: Any) -> str:
    """Searchable text of a JSON value: dict keys, strings, numbers, booleans."""
    parts: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key in sorted(item):
                parts.append(str(key))
                walk(item[key])
        elif isinstance(item, (list, tuple)):
            for sub in item:
                walk(sub)
        elif isinstance(item, bool):
            parts.append("true" if item else "false")
        elif item is not None:
            parts.append(str(item))

    walk(value)
    return " ".join(parts)


def query_terms(query: str) -> list[str]:
    """Distinct query words without stopwords (all words if every one is a stopword)."""
    words = list(dict.fromkeys(_TOKEN.findall(query)))
    content = [w for w in words if w.lower() not in STOPWORDS]
    return content or words


def match_expression(query: str) -> str | None:
    """Turn free text into a safe FTS5 OR-query; ``None`` when nothing is searchable."""
    terms = query_terms(query)
    if not terms:
        return None
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def event_text(kind: str, payload: dict[str, Any]) -> str:
    return f"{kind} {flatten(payload)}"


def internal(subject: str) -> bool:
    """Subjects starting with ``_`` hold bookkeeping facts that are never searchable."""
    return subject.startswith("_")


def fact_text(subject: str, attribute: str, value: Any) -> str:
    return f"{subject} {attribute} {flatten(value)}"


# Facts asserted from one event, oldest first; uses the fact_versions_source index.
EVENT_FACTS_SQL = (
    "SELECT s.subject, s.attribute, v.value FROM fact_versions v"
    " JOIN slots s ON s.slot_id = v.slot_id"
    " WHERE v.source_seq=? AND v.op='assert' AND s.subject NOT LIKE '\\_%' ESCAPE '\\'"
    " ORDER BY v.version_id"
)


def refresh_event(db: sqlite3.Connection, seq: int) -> None:
    """Re-index an event together with every fact ever asserted from it."""
    row = db.execute("SELECT kind, payload FROM events WHERE seq=?", (seq,)).fetchone()
    facts = db.execute(EVENT_FACTS_SQL, (seq,)).fetchall()
    lines = dict.fromkeys(fact_text(f[0], f[1], json.loads(f[2])) for f in facts)
    text = "\n".join([event_text(row[0], json.loads(row[1])), *lines])
    upsert(db, "event", seq, text)


def _stem(word: str) -> str:
    word = word.lower()
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def snippet(text: str, query: str, width: int) -> str:
    """At most ``width`` characters of ``text``, centred on the parts that match ``query``.

    Sentences and lines are ranked by how many distinct query words they contain;
    the best are kept, then widened with their neighbours, and printed in their
    original order with ``…`` marking every gap. Text without any match keeps its
    beginning.
    """
    if width < 1:
        raise ValueError("width must be at least 1")
    if len(text) <= width:
        return text
    terms = {_stem(t) for t in query_terms(query)}
    segments = [s for s in _SEGMENT.split(text) if s.strip()]
    scores = [len(terms & {_stem(w) for w in _TOKEN.findall(s)}) for s in segments]
    if not any(scores):
        return text[: max(0, width - 2)].rstrip() + " …"
    chosen: set[int] = set()
    used = 0

    def fits(i: int) -> bool:
        return used + len(segments[i]) + 3 <= width

    for i in sorted(range(len(segments)), key=lambda i: (-scores[i], i)):
        if scores[i] == 0:
            break
        if fits(i):
            chosen.add(i)
            used += len(segments[i]) + 3
    if not chosen:  # the best sentence alone is too long: cut a window around its first match
        best = max(range(len(segments)), key=lambda i: (scores[i], -i))
        seg = segments[best]
        words = [m for m in _TOKEN.finditer(seg) if _stem(m.group()) in terms]
        start = max(0, min(words[0].start() - width // 3, len(seg) - width + 4))
        return "… " + seg[start : start + width - 4].strip() + " …"
    grew = True
    while grew:
        grew = False
        for i in sorted(chosen):
            for j in (i - 1, i + 1):
                if 0 <= j < len(segments) and j not in chosen and fits(j):
                    chosen.add(j)
                    used += len(segments[j]) + 3
                    grew = True
    parts: list[str] = []
    previous = -1
    for i in sorted(chosen):
        if i != previous + 1:
            parts.append("…")
        parts.append(segments[i].strip())
        previous = i
    if previous != len(segments) - 1:
        parts.append("…")
    result = " ".join(parts)
    return result if len(result) <= width else result[: width - 2].rstrip() + " …"


def upsert(db: sqlite3.Connection, doc_kind: str, ref: int, text: str) -> None:
    row = db.execute(
        "SELECT doc_id FROM search_docs WHERE doc_kind=? AND ref=?", (doc_kind, ref)
    ).fetchone()
    if row is None:
        doc_id = db.execute(
            "INSERT INTO search_docs(doc_kind, ref) VALUES (?, ?)", (doc_kind, ref)
        ).lastrowid
    else:
        doc_id = row[0]
        db.execute("DELETE FROM search WHERE rowid=?", (doc_id,))
    db.execute("INSERT INTO search(rowid, text) VALUES (?, ?)", (doc_id, text))


def remove(db: sqlite3.Connection, doc_kind: str, ref: int) -> None:
    row = db.execute(
        "SELECT doc_id FROM search_docs WHERE doc_kind=? AND ref=?", (doc_kind, ref)
    ).fetchone()
    if row is not None:
        db.execute("DELETE FROM search WHERE rowid=?", (row[0],))
        db.execute("DELETE FROM search_docs WHERE doc_id=?", (row[0],))


def search(
    db: sqlite3.Connection,
    query: str,
    doc_kinds: tuple[str, ...],
    limit: int,
    since: str | None = None,
    until: str | None = None,
) -> list[tuple[str, int, float]]:
    """Return ``(doc_kind, ref, relevance)``, best first, ties broken by doc id.

    ``since`` (inclusive) and ``until`` (exclusive) restrict event documents to
    events dated inside the window; undated events are then excluded. Other
    document kinds are not filtered here.
    """
    expression = match_expression(query)
    if expression is None or not doc_kinds:
        return []
    marks = ",".join("?" * len(doc_kinds))
    window = ""
    params: list[Any] = [expression, *doc_kinds]
    if since is not None or until is not None:
        window = (
            " AND (d.doc_kind != 'event' OR EXISTS (SELECT 1 FROM events e WHERE e.seq = d.ref"
            "  AND e.occurred_at IS NOT NULL AND (? IS NULL OR e.occurred_at >= ?)"
            "  AND (? IS NULL OR e.occurred_at < ?)))"
        )
        params += [since, since, until, until]
    rows = db.execute(
        "SELECT d.doc_kind, d.ref, bm25(search) AS score FROM search "
        "JOIN search_docs d ON d.doc_id = search.rowid "
        f"WHERE search MATCH ? AND d.doc_kind IN ({marks}){window} "
        "ORDER BY score, d.doc_id LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [(r[0], int(r[1]), -float(r[2])) for r in rows]
