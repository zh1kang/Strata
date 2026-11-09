"""Append-only event log."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from . import index
from .errors import ConflictError, NotFoundError
from .store import Store, digest, encode
from .types import Event, IngestResult


def event_text(kind: str, payload: dict[str, Any]) -> str:
    return f"{kind} {index.flatten(payload)}"


def ingest(store: Store, session_id: str, events: Iterable[Event]) -> IngestResult:
    """Insert events idempotently.

    Re-delivering an event with the same key and identical content is a no-op.
    The same key with different content is rejected: events are immutable.
    """
    prepared: list[tuple[Event, str, str]] = []
    seen: dict[str, str] = {}
    for event in events:
        if not event.key or not event.kind or not session_id:
            raise ValueError("event key, kind, and session id must be non-empty")
        if event.session_id != session_id:
            raise ValueError(
                f"event {event.key!r} has session {event.session_id!r}, expected {session_id!r}"
            )
        if not isinstance(event.payload, dict):
            raise ValueError("event payload must be a dict")
        payload = encode(event.payload)
        content = digest(f"{event.kind}\n{payload}")
        if seen.setdefault(event.key, content) != content:
            raise ConflictError(f"event {event.key!r} appears twice with different content")
        prepared.append((event, payload, content))

    inserted = duplicates = 0
    with store.write() as txn:
        db = store.db
        for event, payload, content in prepared:
            existing = db.execute(
                "SELECT content_hash FROM events WHERE event_key=?", (event.key,)
            ).fetchone()
            if existing is not None:
                if existing[0] != content:
                    raise ConflictError(
                        f"event {event.key!r} already exists with different content"
                    )
                duplicates += 1
                continue
            seq = db.execute(
                "INSERT INTO events(event_key, session_id, kind, payload, content_hash, revision)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (event.key, session_id, event.kind, payload, content, txn.revision),
            ).lastrowid
            index.upsert(db, "event", int(seq or 0), event_text(event.kind, event.payload))
            inserted += 1
    return IngestResult(inserted, duplicates)


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(row["event_key"], row["session_id"], row["kind"], json.loads(row["payload"]))


def get(store: Store, key: str) -> Event:
    row = store.db.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()
    if row is None:
        raise NotFoundError(f"unknown event: {key}")
    return _row_to_event(row)


def list_events(store: Store, session_id: str | None = None, limit: int = 100) -> list[Event]:
    if session_id is None:
        rows = store.db.execute("SELECT * FROM events ORDER BY seq LIMIT ?", (limit,))
    else:
        rows = store.db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY seq LIMIT ?", (session_id, limit)
        )
    return [_row_to_event(r) for r in rows.fetchall()]
