"""Versioned fact slots.

A slot is one ``(subject, attribute)`` pair. Each write appends an immutable
version; the slot head points at the latest. A write whose effective value
equals the head's records provenance but does not advance ``changed_revision``
and invalidates nothing (same-value cutoff). Set-valued slots are written with
``mode="add"`` / ``"remove"``, which merge into the head inside the transaction.
Subjects starting with ``_`` are internal bookkeeping (for example topic
membership): tracked like any slot but never indexed for search.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from . import graph, index
from .errors import ConflictError, NotFoundError
from .store import Store, encode
from .types import Fact, FactWrite


@dataclass(frozen=True)
class _Op:
    subject: str
    attribute: str
    op: str
    value: str | None
    source_key: str
    expected_version: int | None
    mode: str = "set"  # how an "assert" combines with the head: set, add, or remove
    missing_ok: bool = False  # a retract of a missing or retracted slot is a no-op


MODES = ("set", "add", "remove", "retract")


def _items(encoded: str) -> list[object]:
    value = json.loads(encoded)
    return list(value) if isinstance(value, list) else [value]


def _combine(op: _Op, head: sqlite3.Row | None) -> str | None:
    """Encoded value an add/remove write leaves in the slot; ``None`` when nothing is written."""
    live = head is not None and head["op"] == "assert"
    if op.value is None or (op.mode == "remove" and not live):
        return None
    present = [encode(item) for item in _items(head["value"])] if live else []
    given = [encode(item) for item in _items(op.value)]
    if op.mode == "add":
        new = list(dict.fromkeys([*present, *given]))
    else:
        new = [item for item in present if item not in given]
    if live and new == present:
        return str(head["value"])
    return encode([json.loads(item) for item in new])


def _validate_name(label: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")


def _apply(store: Store, ops: Sequence[_Op]) -> list[int]:
    db = store.db
    version_ids: list[int] = []
    changed_keys: list[tuple[str, str]] = []
    sources: set[int] = set()  # events whose index document gains a fact
    with store.write() as txn:
        for op in ops:
            slot = db.execute(
                "SELECT slot_id FROM slots WHERE subject=? AND attribute=?",
                (op.subject, op.attribute),
            ).fetchone()
            head = None
            if slot is not None:
                head = db.execute(
                    "SELECT h.version_id, v.op, v.value FROM slot_heads h "
                    "JOIN fact_versions v ON v.version_id = h.version_id WHERE h.slot_id=?",
                    (slot[0],),
                ).fetchone()
            if op.expected_version is not None:
                current = head["version_id"] if head else 0
                if current != op.expected_version:
                    raise ConflictError(
                        f"compare-and-set failed for {op.subject}.{op.attribute}: "
                        f"expected version {op.expected_version}, found {current}"
                    )
            if op.op == "assert" and op.mode != "set":
                combined = _combine(op, head)
                if combined is None:  # removing from a missing slot
                    version_ids.append(int(head["version_id"]) if head else 0)
                    continue
                op = dataclasses.replace(op, value=combined)
            if op.op == "retract":
                if head is None and op.missing_ok:
                    version_ids.append(0)
                    continue
                if head is None:
                    raise NotFoundError(f"no fact to retract: {op.subject}.{op.attribute}")
                if head["op"] == "retract":
                    version_ids.append(int(head["version_id"]))
                    continue
            source = db.execute(
                "SELECT seq FROM events WHERE event_key=?", (op.source_key,)
            ).fetchone()
            if source is None:
                raise NotFoundError(f"unknown source event: {op.source_key}")
            if slot is None:
                slot_id = int(
                    db.execute(
                        "INSERT INTO slots(subject, attribute) VALUES (?, ?)",
                        (op.subject, op.attribute),
                    ).lastrowid
                    or 0
                )
            else:
                slot_id = int(slot[0])
            rev = txn.revision
            version_id = int(
                db.execute(
                    "INSERT INTO fact_versions(slot_id, op, value, source_seq, revision)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (slot_id, op.op, op.value, source[0], rev),
                ).lastrowid
                or 0
            )
            changed = head is None or head["op"] != op.op or head["value"] != op.value
            if head is None:
                db.execute(
                    "INSERT INTO slot_heads(slot_id, version_id, changed_revision)"
                    " VALUES (?, ?, ?)",
                    (slot_id, version_id, rev),
                )
            else:
                db.execute(
                    "UPDATE slot_heads SET version_id=?,"
                    " changed_revision=CASE WHEN ? THEN ? ELSE changed_revision END"
                    " WHERE slot_id=?",
                    (version_id, changed, rev, slot_id),
                )
            if changed:
                db.execute(
                    "INSERT INTO subjects(subject, changed_revision) VALUES (?, ?)"
                    " ON CONFLICT(subject) DO UPDATE SET"
                    " changed_revision=excluded.changed_revision",
                    (op.subject, rev),
                )
                if op.value is not None and not index.internal(op.subject):
                    text = index.fact_text(op.subject, op.attribute, json.loads(op.value))
                    index.upsert(db, "fact", slot_id, text)
                else:
                    index.remove(db, "fact", slot_id)
                changed_keys.append((op.subject, op.attribute))
            version_ids.append(version_id)
            if op.value is not None and not index.internal(op.subject):
                sources.add(int(source[0]))
        for seq in sorted(sources):
            index.refresh_event(db, seq)
        if changed_keys:
            graph.invalidate_for_facts(db, txn.revision, changed_keys)
    return version_ids


def assert_many(store: Store, writes: Sequence[FactWrite]) -> list[int]:
    """Atomically assert several facts; any failure rolls back the whole batch."""
    ops = []
    for w in writes:
        _validate_name("subject", w.subject)
        _validate_name("attribute", w.attribute)
        _validate_name("source_key", w.source_key)
        if w.mode not in MODES:
            raise ValueError(f"unknown fact write mode {w.mode!r}; expected one of {MODES}")
        if w.mode == "retract":
            ops.append(
                _Op(
                    w.subject,
                    w.attribute,
                    "retract",
                    None,
                    w.source_key,
                    w.expected_version,
                    missing_ok=True,
                )
            )
            continue
        ops.append(
            _Op(
                w.subject,
                w.attribute,
                "assert",
                encode(w.value),
                w.source_key,
                w.expected_version,
                w.mode,
            )
        )
    return _apply(store, ops)


def retract(
    store: Store,
    subject: str,
    attribute: str,
    source_key: str,
    expected_version: int | None = None,
) -> int:
    _validate_name("subject", subject)
    _validate_name("attribute", attribute)
    _validate_name("source_key", source_key)
    return _apply(store, [_Op(subject, attribute, "retract", None, source_key, expected_version)])[
        0
    ]


def _row_to_fact(row: sqlite3.Row) -> Fact:
    value = json.loads(row["value"]) if row["value"] is not None else None
    return Fact(
        row["subject"],
        row["attribute"],
        value,
        int(row["version_id"]),
        row["op"],
        row["event_key"],
        int(row["revision"]),
        row["occurred_at"],
    )


_FACT_SELECT = (
    "SELECT s.subject, s.attribute, v.version_id, v.op, v.value, v.revision, e.event_key,"
    " e.occurred_at "
    "FROM slots s JOIN fact_versions v ON v.slot_id = s.slot_id "
    "JOIN events e ON e.seq = v.source_seq "
)


def get(store: Store, subject: str, attribute: str) -> Fact | None:
    """The live fact for a slot, or ``None`` if missing or retracted."""
    row = store.db.execute(
        _FACT_SELECT
        + "JOIN slot_heads h ON h.version_id = v.version_id WHERE s.subject=? AND s.attribute=?",
        (subject, attribute),
    ).fetchone()
    if row is None or row["op"] != "assert":
        return None
    return _row_to_fact(row)


def facts_of(store: Store, subject: str) -> list[Fact]:
    rows = store.db.execute(
        _FACT_SELECT + "JOIN slot_heads h ON h.version_id = v.version_id "
        "WHERE s.subject=? AND v.op='assert' ORDER BY s.attribute",
        (subject,),
    ).fetchall()
    return [_row_to_fact(r) for r in rows]


def history(store: Store, subject: str, attribute: str) -> list[Fact]:
    rows = store.db.execute(
        _FACT_SELECT + "WHERE s.subject=? AND s.attribute=? ORDER BY v.version_id",
        (subject, attribute),
    ).fetchall()
    return [_row_to_fact(r) for r in rows]


def head_version(store: Store, subject: str, attribute: str) -> int:
    """Current head version id, or 0 when the slot has no versions."""
    row = store.db.execute(
        "SELECT h.version_id FROM slots s JOIN slot_heads h ON h.slot_id = s.slot_id "
        "WHERE s.subject=? AND s.attribute=?",
        (subject, attribute),
    ).fetchone()
    return int(row[0]) if row else 0
