"""Incremental FTS5 index.

Index rows carry text only. Whether a hit is current, stale, or raw is always
resolved against the live tables at query time, never stored in the index.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

_TOKEN = re.compile(r"\w+", re.UNICODE)


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


def match_expression(query: str) -> str | None:
    """Turn free text into a safe FTS5 OR-query; ``None`` when nothing is searchable."""
    tokens: list[str] = []
    for token in _TOKEN.findall(query):
        if token not in tokens:
            tokens.append(token)
    if not tokens:
        return None
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


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
    db: sqlite3.Connection, query: str, doc_kinds: tuple[str, ...], limit: int
) -> list[tuple[str, int, float]]:
    """Return ``(doc_kind, ref, relevance)``, best first, ties broken by doc id."""
    expression = match_expression(query)
    if expression is None or not doc_kinds:
        return []
    marks = ",".join("?" * len(doc_kinds))
    rows = db.execute(
        "SELECT d.doc_kind, d.ref, bm25(search) AS score FROM search "
        "JOIN search_docs d ON d.doc_id = search.rowid "
        f"WHERE search MATCH ? AND d.doc_kind IN ({marks}) "
        "ORDER BY score, d.doc_id LIMIT ?",
        (expression, *doc_kinds, limit),
    ).fetchall()
    return [(r[0], int(r[1]), -float(r[2])) for r in rows]
