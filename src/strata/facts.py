"""Versioned fact slots.

A slot is one ``(subject, attribute)`` pair. Each write appends an immutable
version; the slot head points at the latest. A write whose effective value
equals the head's records provenance but does not advance ``changed_revision``
and invalidates nothing (same-value cutoff). Set-valued slots are written with
``mode="add"`` / ``"remove"``, which merge into the head inside the transaction.
Subjects starting with ``_`` are internal bookkeeping (for example topic
membership): tracked like any slot but never indexed for search.

Conditional facts: a write may name premise slots. Their values when the batch
commits are recorded with the version, and :func:`status` reports the fact as
``unverified`` once a premise holds another value. A ``contingent`` write stores
what a slot becomes when its premises change; it fires inside the transaction
that changes a premise, so dependent facts never lag behind their premises.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from . import graph, index
from .errors import ConflictError, NotFoundError
from .store import Store, WriteTxn, encode
from .types import Fact, FactStatus, FactWrite


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
    premises: tuple[tuple[str, str], ...] = ()


MODES = ("set", "add", "remove", "retract", "contingent")
# Contingencies live in internal slots: subject CONTINGENT, attribute encode([subject, attribute]).
CONTINGENT = "_contingent"
_MAX_CASCADE = 1000  # contingency firings per transaction; each one uses a contingency up


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


class _Batch:
    """One write transaction's bookkeeping."""

    def __init__(self, db: sqlite3.Connection, txn: WriteTxn) -> None:
        self.db = db
        self.txn = txn
        self.changed: list[tuple[str, str]] = []
        self.sources: set[int] = set()  # events whose index document gains a fact
        # (version id, premises, slot id, subject, attribute, previous head version, changed)
        self.conditional: list[
            tuple[int, tuple[tuple[str, str], ...], int, str, str, int | None, bool]
        ] = []

    def head(self, subject: str, attribute: str) -> tuple[int | None, sqlite3.Row | None]:
        slot = self.db.execute(
            "SELECT slot_id FROM slots WHERE subject=? AND attribute=?", (subject, attribute)
        ).fetchone()
        if slot is None:
            return None, None
        head = self.db.execute(
            "SELECT h.version_id, v.op, v.value, v.source_seq FROM slot_heads h "
            "JOIN fact_versions v ON v.version_id = h.version_id WHERE h.slot_id=?",
            (slot[0],),
        ).fetchone()
        return int(slot[0]), head

    def apply(self, op: _Op) -> int:
        db = self.db
        slot_id, head = self.head(op.subject, op.attribute)
        if op.expected_version is not None:
            current = head["version_id"] if head else 0
            if current != op.expected_version:
                raise ConflictError(
                    f"compare-and-set failed for {op.subject}.{op.attribute}: "
                    f"expected version {op.expected_version}, found {current}"
                )
        if op.op == "assert" and op.mode in ("add", "remove"):
            combined = _combine(op, head)
            if combined is None:  # removing from a missing slot
                return int(head["version_id"]) if head else 0
            op = dataclasses.replace(op, value=combined)
        if op.op == "retract":
            if head is None and op.missing_ok:
                return 0
            if head is None:
                raise NotFoundError(f"no fact to retract: {op.subject}.{op.attribute}")
            if head["op"] == "retract":
                return int(head["version_id"])
        source = db.execute("SELECT seq FROM events WHERE event_key=?", (op.source_key,)).fetchone()
        if source is None:
            raise NotFoundError(f"unknown source event: {op.source_key}")
        return self.write(op, slot_id, head, int(source[0]))

    def write(self, op: _Op, slot_id: int | None, head: sqlite3.Row | None, seq: int) -> int:
        db = self.db
        if slot_id is None:
            slot_id = int(
                db.execute(
                    "INSERT INTO slots(subject, attribute) VALUES (?, ?)",
                    (op.subject, op.attribute),
                ).lastrowid
                or 0
            )
        rev = self.txn.revision
        version_id = int(
            db.execute(
                "INSERT INTO fact_versions(slot_id, op, value, source_seq, revision)"
                " VALUES (?, ?, ?, ?, ?)",
                (slot_id, op.op, op.value, seq, rev),
            ).lastrowid
            or 0
        )
        changed = head is None or head["op"] != op.op or head["value"] != op.value
        if head is None:
            db.execute(
                "INSERT INTO slot_heads(slot_id, version_id, changed_revision) VALUES (?, ?, ?)",
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
                " ON CONFLICT(subject) DO UPDATE SET changed_revision=excluded.changed_revision",
                (op.subject, rev),
            )
            if op.value is not None and not index.internal(op.subject):
                text = index.fact_text(op.subject, op.attribute, json.loads(op.value))
                index.upsert(db, "fact", slot_id, text)
            else:
                index.remove(db, "fact", slot_id)
            self.changed.append((op.subject, op.attribute))
        if op.value is not None and not index.internal(op.subject):
            self.sources.add(seq)
        previous = int(head["version_id"]) if head is not None else None
        premises = op.premises if op.op == "assert" else ()
        if premises or (previous is not None and _premises_of(db, previous)):
            self.conditional.append(
                (version_id, premises, slot_id, op.subject, op.attribute, previous, changed)
            )
        return version_id

    def record_premises(self) -> None:
        """Snapshot the premises of the versions written so far, as of now.

        A version whose value equals the previous head's but whose premises (or
        their recorded values) differ still counts as a change: its status may differ.
        """
        db = self.db
        for entry in self.conditional:
            version_id, premises, slot_id, subject, attribute, previous, changed = entry
            for p_subject, p_attribute in premises:
                db.execute(
                    "INSERT OR REPLACE INTO fact_premises(version_id, subject, attribute, value)"
                    " VALUES (?, ?, ?, ?)",
                    (version_id, p_subject, p_attribute, _live_value(db, p_subject, p_attribute)),
                )
            if changed or previous is None:
                continue
            if _premises_of(db, previous) == _premises_of(db, version_id):
                continue
            rev = self.txn.revision
            db.execute("UPDATE slot_heads SET changed_revision=? WHERE slot_id=?", (rev, slot_id))
            db.execute(
                "INSERT INTO subjects(subject, changed_revision) VALUES (?, ?)"
                " ON CONFLICT(subject) DO UPDATE SET changed_revision=excluded.changed_revision",
                (subject, rev),
            )
            self.changed.append((subject, attribute))
        self.conditional.clear()

    def cascade(self) -> None:
        """Fire every contingency whose premise moved away from its recorded value."""
        done = 0
        position = 0
        while position < len(self.changed):
            subject, attribute = self.changed[position]
            position += 1
            if index.internal(subject):
                continue
            rows = self.db.execute(
                "SELECT s.attribute AS target, v.value, fp.value AS recorded"
                " FROM fact_premises fp"
                " JOIN slot_heads h ON h.version_id = fp.version_id"
                " JOIN slots s ON s.slot_id = h.slot_id"
                " JOIN fact_versions v ON v.version_id = h.version_id"
                " WHERE fp.subject=? AND fp.attribute=? AND s.subject=? AND v.op='assert'"
                " ORDER BY s.slot_id",
                (subject, attribute, CONTINGENT),
            ).fetchall()
            if not rows:
                continue
            live = _live_value(self.db, subject, attribute)
            _, premise_head = self.head(subject, attribute)
            seq = int(premise_head["source_seq"])
            for row in rows:
                if row["recorded"] == live:
                    continue
                done += 1
                if done > _MAX_CASCADE:
                    raise ConflictError("contingency cascade did not settle")
                target_subject, target_attribute = json.loads(row["target"])
                stored = json.loads(row["value"])
                premises = tuple((s, a) for s, a in stored["premises"])
                spent = _Op(CONTINGENT, row["target"], "retract", None, "", None)
                slot_id, head = self.head(CONTINGENT, row["target"])
                self.write(spent, slot_id, head, seq)
                fired = _Op(
                    target_subject,
                    target_attribute,
                    "assert",
                    encode(stored["value"]),
                    "",
                    None,
                    premises=premises,
                )
                slot_id, head = self.head(target_subject, target_attribute)
                self.write(fired, slot_id, head, seq)
                self.record_premises()


def _premises_of(db: sqlite3.Connection, version_id: int) -> list[tuple[str, str, str | None]]:
    return [
        (r[0], r[1], r[2])
        for r in db.execute(
            "SELECT subject, attribute, value FROM fact_premises WHERE version_id=?"
            " ORDER BY subject, attribute",
            (version_id,),
        )
    ]


def _live_value(db: sqlite3.Connection, subject: str, attribute: str) -> str | None:
    """Encoded live value of a slot; ``None`` when missing or retracted."""
    row = db.execute(
        "SELECT v.op, v.value FROM slots s JOIN slot_heads h ON h.slot_id = s.slot_id"
        " JOIN fact_versions v ON v.version_id = h.version_id"
        " WHERE s.subject=? AND s.attribute=?",
        (subject, attribute),
    ).fetchone()
    return None if row is None or row["op"] != "assert" else str(row["value"])


def _apply(store: Store, ops: Sequence[_Op]) -> list[int]:
    db = store.db
    version_ids: list[int] = []
    with store.write() as txn:
        batch = _Batch(db, txn)
        for op in ops:
            version_ids.append(batch.apply(op))
        batch.record_premises()
        batch.cascade()
        for seq in sorted(batch.sources):
            index.refresh_event(db, seq)
        if batch.changed:
            graph.invalidate_for_facts(db, txn.revision, batch.changed)
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
        premises = tuple(dict.fromkeys((str(s), str(a)) for s, a in w.premises))
        for p_subject, p_attribute in premises:
            _validate_name("premise subject", p_subject)
            _validate_name("premise attribute", p_attribute)
            if (p_subject, p_attribute) == (w.subject, w.attribute):
                raise ValueError(f"{w.subject}.{w.attribute} cannot be its own premise")
        if w.mode == "contingent":
            if not premises:
                raise ValueError("a contingent write needs at least one premise")
            stored = {"value": w.value, "premises": [list(p) for p in premises]}
            ops.append(
                _Op(
                    CONTINGENT,
                    encode([w.subject, w.attribute]),
                    "assert",
                    encode(stored),
                    w.source_key,
                    None,
                    premises=premises,
                )
            )
            continue
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
                premises=premises,
            )
        )
    return _apply(store, ops)


def premises(db: sqlite3.Connection, subject: str, attribute: str) -> list[tuple[str, str]]:
    """Premise slots of a slot's head version."""
    return [
        (r[0], r[1])
        for r in db.execute(
            "SELECT fp.subject, fp.attribute FROM slots s"
            " JOIN slot_heads h ON h.slot_id = s.slot_id"
            " JOIN fact_premises fp ON fp.version_id = h.version_id"
            " WHERE s.subject=? AND s.attribute=? ORDER BY fp.subject, fp.attribute",
            (subject, attribute),
        )
    ]


def premise_closure(db: sqlite3.Connection, subject: str, attribute: str) -> list[tuple[str, str]]:
    """Every slot the status of ``subject.attribute`` depends on, transitively."""
    seen: dict[tuple[str, str], None] = {}
    todo = premises(db, subject, attribute)
    while todo:
        slot = todo.pop()
        if slot in seen or slot == (subject, attribute):
            continue
        seen[slot] = None
        todo.extend(premises(db, *slot))
    return list(seen)


def status(db: sqlite3.Connection, subject: str, attribute: str) -> FactStatus:
    """``current`` unless a premise changed since the head was written or is itself unverified."""
    return _status(db, subject, attribute, set())


def _status(
    db: sqlite3.Connection, subject: str, attribute: str, visiting: set[tuple[str, str]]
) -> FactStatus:
    visiting = visiting | {(subject, attribute)}
    rows = db.execute(
        "SELECT fp.subject, fp.attribute, fp.value FROM slots s"
        " JOIN slot_heads h ON h.slot_id = s.slot_id"
        " JOIN fact_premises fp ON fp.version_id = h.version_id"
        " WHERE s.subject=? AND s.attribute=? ORDER BY fp.subject, fp.attribute",
        (subject, attribute),
    ).fetchall()
    reasons: list[str] = []
    for p_subject, p_attribute, recorded in rows:
        name = f"{p_subject}.{p_attribute}"
        live = _live_value(db, p_subject, p_attribute)
        if live != recorded:
            old = "nothing" if recorded is None else recorded
            new = "nothing" if live is None else live
            reasons.append(f"{name} changed from {old} to {new}")
        elif (p_subject, p_attribute) not in visiting:
            inner = _status(db, p_subject, p_attribute, visiting)
            if inner.status != "current":
                reasons.append(f"{name} is itself unverified ({'; '.join(inner.reasons)})")
    return FactStatus("unverified" if reasons else "current", tuple(reasons))


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
