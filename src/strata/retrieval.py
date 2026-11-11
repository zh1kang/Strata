"""Provenance-carrying retrieval with a safe fallback.

Safety rules:

* A derived node is only ever returned as ``current`` while its state is
  ``valid``. Stale, suspect, pending, and failed nodes are withheld.
* ``include_stale=True`` returns withheld nodes explicitly labelled ``stale``.
* When current memory cannot answer (no current hit, or a relevant derived item
  was withheld, including one that was never built) the retriever falls back to
  raw events labelled ``raw``; they are evidence, not verified current state.
* ``events=N`` always adds up to N raw events next to current memory: first the
  source event of each matched fact, interleaved with plain event search hits.
  Facts say what is current; their source events keep the detail and the date.
* ``lazy_budget`` lets a query repair the dirty nodes it needs on demand, within
  that budget, before deciding what to withhold.
* ``since`` / ``until`` keep only events, and facts whose source event, dated
  inside ``[since, until)``. Derived nodes are not filtered.
* A fact whose premise changed (see :mod:`strata.facts`) is returned labelled
  ``unverified`` with the reason, never as ``current``, and triggers the fallback
  like a withheld node does.
"""

from __future__ import annotations

import json
import sqlite3
from itertools import zip_longest
from typing import Any

from . import facts, graph, index
from .errors import NotFoundError
from .events import normalise_time
from .maintenance import Engine
from .store import Store
from .types import Budget, NodeRead, RecallItem, RecallResult


class Retriever:
    def __init__(self, store: Store, engine: Engine) -> None:
        self.store = store
        self.engine = engine

    # ------------------------------------------------------------------ resolution

    def _fact_item(self, slot_id: int, score: float) -> RecallItem | None:
        row = self.store.db.execute(
            "SELECT s.subject, s.attribute, v.version_id, v.value, e.event_key, e.occurred_at"
            " FROM slots s"
            " JOIN slot_heads h ON h.slot_id = s.slot_id"
            " JOIN fact_versions v ON v.version_id = h.version_id"
            " JOIN events e ON e.seq = v.source_seq"
            " WHERE s.slot_id=? AND v.op='assert'",
            (slot_id,),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value"])
        text = f"{row['subject']}.{row['attribute']} = {row['value']}"
        state = facts.status(self.store.db, row["subject"], row["attribute"])
        if state.status != "current":
            text += f" (unverified: {'; '.join(state.reasons)})"
        return RecallItem(
            text,
            state.status,
            "fact",
            f"{row['subject']}/{row['attribute']}",
            score,
            (
                f"fact:{row['subject']}/{row['attribute']}@{row['version_id']}",
                f"event:{row['event_key']}",
            ),
            value,
            row["occurred_at"],
        )

    def _node_item(self, row: sqlite3.Row, score: float, status: str) -> RecallItem:
        return RecallItem(
            row["value"],
            status,
            row["kind"],
            row["node_key"],
            score,
            graph.provenance(self.store.db, int(row["node_id"])),
            json.loads(row["value"]),
        )

    def _node_row(self, node_id: int) -> sqlite3.Row | None:
        return self.store.db.execute(
            "SELECT node_id, kind, node_key, state, value FROM nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()

    def _require_node_row(self, node_id: int) -> sqlite3.Row:
        row = self._node_row(node_id)
        if row is None:
            raise NotFoundError(f"node {node_id} no longer exists")
        return row

    @staticmethod
    def _event_item(row: sqlite3.Row, score: float) -> RecallItem:
        payload = json.loads(row["payload"])
        return RecallItem(
            f"{row['kind']} {index.flatten(payload)}",
            "raw",
            "event",
            row["event_key"],
            score,
            (f"event:{row['event_key']}",),
            payload,
            row["occurred_at"],
        )

    def _raw_items(
        self, query: str, limit: int, since: str | None = None, until: str | None = None
    ) -> list[RecallItem]:
        items = []
        for _, seq, score in index.search(self.store.db, query, ("event",), limit, since, until):
            row = self.store.db.execute(
                "SELECT event_key, kind, payload, occurred_at FROM events WHERE seq=?", (seq,)
            ).fetchone()
            items.append(self._event_item(row, score))
        return items

    def _source_items(self, current: list[RecallItem]) -> list[RecallItem]:
        """Source events of current facts, in the facts' rank order."""
        items = []
        for item in current:
            if item.kind != "fact":
                continue
            for ref in item.provenance:
                if not ref.startswith("event:"):
                    continue
                row = self.store.db.execute(
                    "SELECT event_key, kind, payload, occurred_at FROM events WHERE event_key=?",
                    (ref.removeprefix("event:"),),
                ).fetchone()
                if row is not None:
                    items.append(self._event_item(row, item.score))
        return items

    @staticmethod
    def _merge_events(
        sources: list[RecallItem], searched: list[RecallItem], limit: int
    ) -> list[RecallItem]:
        merged: list[RecallItem] = []
        seen: set[str] = set()
        for pair in zip_longest(sources, searched):
            for item in pair:
                if item is not None and item.key not in seen and len(merged) < limit:
                    seen.add(item.key)
                    merged.append(item)
        return merged

    def search_events(
        self, query: str, limit: int = 10, *, since: str | None = None, until: str | None = None
    ) -> list[RecallItem]:
        """Plain append-only search over raw events (no derived memory involved)."""
        return self._raw_items(query, limit, normalise_time(since), normalise_time(until))

    # ------------------------------------------------------------------ recall

    def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        include_stale: bool = False,
        fallback: bool = True,
        lazy_budget: float | None = None,
        record_access: bool = True,
        events: int = 0,
        since: str | None = None,
        until: str | None = None,
    ) -> RecallResult:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if events < 0:
            raise ValueError("events must be non-negative")
        since, until = normalise_time(since), normalise_time(until)
        windowed = since is not None or until is not None

        def in_window(at: str | None) -> bool:
            if not windowed:
                return True
            return (
                at is not None and (since is None or at >= since) and (until is None or at < until)
            )

        hits = index.search(self.store.db, query, ("fact", "node"), limit * 5)
        node_ids = [ref for kind, ref, _ in hits if kind == "node"]

        refreshed = 0
        if lazy_budget is not None:
            dirty = [n for n in node_ids if self._is_dirty(n)]
            if dirty:
                report = self.engine.maintain(
                    Budget(lazy_budget), only=graph.upstream_dirty(self.store.db, dirty)
                )
                refreshed = report.jobs + report.verified

        current: list[RecallItem] = []
        stale: list[RecallItem] = []
        unverified: list[RecallItem] = []
        suppressed = 0
        for kind, ref, score in hits:
            if kind == "fact":
                item = self._fact_item(ref, score)
                if item is None or not in_window(item.at):
                    continue
                if item.status == "current":
                    current.append(item)
                else:
                    suppressed += 1
                    unverified.append(item)
                continue
            row = self._node_row(ref)
            if row is None:
                continue
            if row["state"] == "valid":
                current.append(self._node_item(row, score, "current"))
            else:
                suppressed += 1
                if include_stale and row["value"] is not None:
                    stale.append(self._node_item(row, score, "stale"))
        if record_access and node_ids:
            self.engine.touch(node_ids)

        warnings: list[str] = []
        if suppressed > len(unverified):
            warnings.append(
                f"{suppressed - len(unverified)} derived item(s) withheld because they are not"
                " up to date"
            )
        if unverified:
            warnings.append(
                f"{len(unverified)} fact(s) flagged unverified because a premise changed"
            )
        current, stale, unverified = current[:limit], stale[:limit], unverified[:limit]
        use_fallback = fallback and (not current or suppressed > 0)
        raw: list[RecallItem] = []
        quota = max(events, limit if use_fallback else 0)
        if quota:
            sources = [i for i in self._source_items(current) if in_window(i.at)] if events else []
            searched = self._raw_items(query, quota, since, until)
            raw = self._merge_events(sources, searched, quota)
            if raw:
                warnings.append("raw event matches are unverified evidence, not current state")
            elif current:
                warnings.append("no raw event evidence matched the query")
            else:
                warnings.append("no current memory or raw evidence matched")
        items = current + unverified + stale + raw
        return RecallResult(tuple(items), use_fallback, suppressed, refreshed, tuple(warnings))

    def _is_dirty(self, node_id: int) -> bool:
        row = self._node_row(node_id)
        return row is not None and row["state"] != "valid"

    # ------------------------------------------------------------------ direct node reads

    def read_node(
        self,
        kind: str,
        key: str,
        *,
        refresh_budget: float | None = None,
        record_access: bool = True,
    ) -> NodeRead:
        """Read a node by name; the value is returned only if it is valid."""
        node_id = self.engine.node_id(kind, key)
        if record_access:
            self.engine.touch([node_id])
        refreshed = False
        row = self._require_node_row(node_id)
        if row["state"] != "valid" and refresh_budget is not None:
            report = self.engine.maintain(
                Budget(refresh_budget), only=graph.upstream_dirty(self.store.db, [node_id])
            )
            refreshed = (report.jobs + report.verified) > 0
            row = self._require_node_row(node_id)
        if row["state"] == "valid":
            value: Any = json.loads(row["value"])
            return NodeRead(kind, key, "current", row["state"], value, refreshed)
        return NodeRead(kind, key, "unavailable", row["state"], None, refreshed)
