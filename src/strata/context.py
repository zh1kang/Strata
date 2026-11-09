"""Tracked read context handed to derivation functions.

Every read is recorded with the revision observed, so invalidation depends on
what a derivation actually read (including reads of slots that did not exist yet)
and never on citations produced by a model.
"""

from __future__ import annotations

import json
from typing import Any

from . import graph
from .errors import DependencyNotReadyError, LevelViolationError, NotFoundError
from .store import Store


class Context:
    def __init__(self, store: Store, node_id: int, kind: str, key: str, level: int) -> None:
        self._store = store
        self.node_id = node_id
        self.kind = kind
        self.key = key
        self.level = level
        # (dep_kind, subject, attribute, node id) -> first observed revision
        self.observed: dict[tuple[str, str, str, int], int] = {}
        # First engine signal raised by ``node()``. A derivation that swallows it (for example
        # with a broad ``except``) would otherwise commit a result that never recorded that
        # input, so the engine re-raises it once the derivation returns.
        self._signal: Exception | None = None

    def raise_if_signalled(self) -> None:
        """Re-raise an engine signal the derivation may have swallowed."""
        if self._signal is not None:
            raise self._signal

    def _signal_error(self, error: Exception) -> Exception:
        if self._signal is None:
            self._signal = error
        return error

    def _observe(self, kind: str, subject: str, attribute: str, node: int, revision: int) -> None:
        self.observed.setdefault((kind, subject, attribute, node), revision)

    def slot(self, subject: str, attribute: str, default: Any = None) -> Any:
        """Live value of a slot. Missing and retracted slots return ``default`` but are tracked."""
        row = self._store.db.execute(
            "SELECT h.changed_revision, v.op, v.value FROM slots s"
            " JOIN slot_heads h ON h.slot_id = s.slot_id"
            " JOIN fact_versions v ON v.version_id = h.version_id"
            " WHERE s.subject=? AND s.attribute=?",
            (subject, attribute),
        ).fetchone()
        if row is None:
            self._observe(graph.SLOT, subject, attribute, 0, 0)
            return default
        self._observe(graph.SLOT, subject, attribute, 0, int(row["changed_revision"]))
        if row["op"] != "assert":
            return default
        return json.loads(row["value"])

    def subject(self, subject: str) -> dict[str, Any]:
        """All live attributes of a subject.

        Depends on the subject as a whole, so a slot created later (a phantom
        dependency) invalidates the reader.
        """
        db = self._store.db
        revision = graph.current_revision(db, graph.SUBJECT, subject, "", 0)
        self._observe(graph.SUBJECT, subject, "", 0, revision)  # read revision first: safe
        rows = db.execute(
            "SELECT s.attribute, v.value FROM slots s"
            " JOIN slot_heads h ON h.slot_id = s.slot_id"
            " JOIN fact_versions v ON v.version_id = h.version_id"
            " WHERE s.subject=? AND v.op='assert' ORDER BY s.attribute",
            (subject,),
        ).fetchall()
        return {r["attribute"]: json.loads(r["value"]) for r in rows}

    def history(self, subject: str, attribute: str) -> list[dict[str, Any]]:
        """Value changes of a slot, oldest first, tracked like :meth:`slot`.

        Each entry is ``{"op", "value", "source"}`` for the first version of a run of
        identical versions. Re-asserting the current value does not move the slot's
        revision, so it must not change this output either.
        """
        db = self._store.db
        head = db.execute(
            "SELECT h.changed_revision FROM slots s JOIN slot_heads h ON h.slot_id = s.slot_id"
            " WHERE s.subject=? AND s.attribute=?",
            (subject, attribute),
        ).fetchone()
        self._observe(graph.SLOT, subject, attribute, 0, int(head[0]) if head else 0)
        rows = db.execute(
            "SELECT v.op, v.value, e.event_key FROM slots s"
            " JOIN fact_versions v ON v.slot_id = s.slot_id"
            " JOIN events e ON e.seq = v.source_seq"
            " WHERE s.subject=? AND s.attribute=? ORDER BY v.version_id",
            (subject, attribute),
        ).fetchall()
        changes: list[dict[str, Any]] = []
        last: tuple[str, str | None] | None = None
        for r in rows:
            if (r["op"], r["value"]) == last:
                continue
            last = (r["op"], r["value"])
            value = json.loads(r["value"]) if r["value"] is not None else None
            changes.append({"op": r["op"], "value": value, "source": r["event_key"]})
        return changes

    def event(self, key: str) -> dict[str, Any]:
        """Payload of a stored event. Events are immutable, so this read is not tracked."""
        row = self._store.db.execute(
            "SELECT payload FROM events WHERE event_key=?", (key,)
        ).fetchone()
        if row is None:
            raise self._signal_error(NotFoundError(f"unknown event: {key}"))
        return dict(json.loads(row["payload"]))

    def node(self, kind: str, key: str) -> Any:
        """Value of a strictly lower-level node. It must currently be valid."""
        row = self._store.db.execute(
            "SELECT node_id, level, state, value, changed_revision FROM nodes"
            " WHERE kind=? AND node_key=?",
            (kind, key),
        ).fetchone()
        if row is None:
            raise self._signal_error(NotFoundError(f"unknown node: {kind}/{key}"))
        if row["level"] >= self.level:
            raise self._signal_error(
                LevelViolationError(
                    f"{self.kind}/{self.key} (level {self.level}) may only read lower levels, "
                    f"but {kind}/{key} is level {row['level']}"
                )
            )
        if row["state"] != "valid":
            raise self._signal_error(DependencyNotReadyError(f"{kind}/{key} is {row['state']}"))
        self._observe(graph.NODE, "", "", int(row["node_id"]), int(row["changed_revision"]))
        return json.loads(row["value"])

    def observed_list(self) -> list[graph.ObservedDep]:
        return [
            (kind, subject, attribute, node, revision)
            for (kind, subject, attribute, node), revision in sorted(self.observed.items())
        ]
