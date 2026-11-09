"""Public facade over the store, event log, fact slots, graph, scheduler, and retrieval."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from types import TracebackType
from typing import Any

from . import events, facts, graph
from .context import Context
from .maintenance import Engine
from .retrieval import Retriever
from .store import Store
from .types import (
    Budget,
    Dependency,
    Event,
    Fact,
    FactWrite,
    IngestResult,
    MaintenanceReport,
    NodeInfo,
    NodeRead,
    Policy,
    RecallItem,
    RecallResult,
)


class Memory:
    """One connection to a Strata database.

    A ``Memory`` is not thread-safe. Several ``Memory`` objects (threads or
    processes) may share one database file; writes are serialised by SQLite and
    rebuilds use leases plus optimistic commit checks.
    """

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float] = time.time,
        lease_ttl: float = 300.0,
        max_attempts: int = 3,
    ) -> None:
        self.store = Store(path)
        self.engine = Engine(
            self.store, clock=clock, lease_ttl=lease_ttl, max_attempts=max_attempts
        )
        self.retriever = Retriever(self.store, self.engine)

    @classmethod
    def open(cls, path: str, **options: Any) -> Memory:
        return cls(path, **options)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> Memory:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ events

    def ingest(self, session_id: str, new_events: Iterable[Event]) -> IngestResult:
        return events.ingest(self.store, session_id, new_events)

    def event(self, key: str) -> Event:
        return events.get(self.store, key)

    def events(self, session_id: str | None = None, limit: int = 100) -> list[Event]:
        return events.list_events(self.store, session_id, limit)

    # ------------------------------------------------------------------ facts

    def assert_fact(
        self,
        subject: str,
        attribute: str,
        value: Any,
        source_key: str,
        expected_version: int | None = None,
    ) -> int:
        """Assert a fact; returns the new version id. See :class:`FactWrite` for CAS rules."""
        return self.assert_facts(
            [FactWrite(subject, attribute, value, source_key, expected_version)]
        )[0]

    def assert_facts(self, writes: Sequence[FactWrite]) -> list[int]:
        return facts.assert_many(self.store, writes)

    def retract_fact(
        self,
        subject: str,
        attribute: str,
        source_key: str,
        expected_version: int | None = None,
    ) -> int:
        return facts.retract(self.store, subject, attribute, source_key, expected_version)

    def get_fact(self, subject: str, attribute: str) -> Fact | None:
        return facts.get(self.store, subject, attribute)

    def facts(self, subject: str) -> list[Fact]:
        return facts.facts_of(self.store, subject)

    def fact_history(self, subject: str, attribute: str) -> list[Fact]:
        return facts.history(self.store, subject, attribute)

    def head_version(self, subject: str, attribute: str) -> int:
        return facts.head_version(self.store, subject, attribute)

    # ------------------------------------------------------------------ derived nodes

    def define(
        self,
        kind: str,
        key: str,
        level: int,
        fn: Callable[[Context], Any],
        cost: float = 1.0,
        version: str = "1",
        search_text: Callable[[Any], str] | None = None,
    ) -> None:
        self.engine.define(kind, key, level, fn, cost, version, search_text)

    def maintain(
        self, budget: Budget | float | None = None, policy: Policy | str = Policy.PRIORITY
    ) -> MaintenanceReport:
        return self.engine.maintain(budget, policy)

    def rebuild(
        self, budget: Budget | float | None = None, policy: Policy | str = Policy.FIFO
    ) -> MaintenanceReport:
        """Discard all incremental knowledge: mark every built node stale and recompute."""
        self.engine.mark_all_stale()
        return self.engine.maintain(budget, policy)

    def retry_failed(self) -> int:
        return self.engine.retry_failed()

    def node(self, kind: str, key: str) -> NodeInfo:
        return self.engine.get_node(kind, key)

    def nodes(self, state: str | None = None) -> list[NodeInfo]:
        return self.engine.list_nodes(state)

    def dependencies(self, kind: str, key: str) -> list[Dependency]:
        return graph.dependencies(self.store.db, self.engine.node_id(kind, key))

    def dependents(self, kind: str, key: str) -> list[tuple[str, str]]:
        return graph.dependents(self.store.db, self.engine.node_id(kind, key))

    def explain(self, kind: str, key: str) -> dict[str, Any]:
        info = self.node(kind, key)
        return {
            "kind": kind,
            "key": key,
            "state": info.state,
            "level": info.level,
            "dependencies": [
                {"kind": d.kind, "ref": d.ref, "observed_revision": d.observed_revision}
                for d in self.dependencies(kind, key)
            ],
            "dependents": [f"{k}/{n}" for k, n in self.dependents(kind, key)],
            "provenance": list(graph.provenance(self.store.db, self.engine.node_id(kind, key))),
        }

    # ------------------------------------------------------------------ retrieval

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
    ) -> RecallResult:
        return self.retriever.recall(
            query,
            limit=limit,
            include_stale=include_stale,
            fallback=fallback,
            lazy_budget=lazy_budget,
            record_access=record_access,
            events=events,
        )

    def read_node(
        self,
        kind: str,
        key: str,
        *,
        refresh_budget: float | None = None,
        record_access: bool = True,
    ) -> NodeRead:
        return self.retriever.read_node(
            kind, key, refresh_budget=refresh_budget, record_access=record_access
        )

    def search_events(self, query: str, limit: int = 10) -> list[RecallItem]:
        return self.retriever.search_events(query, limit)

    # ------------------------------------------------------------------ introspection

    def status(self) -> dict[str, Any]:
        db = self.store.db
        counts = {
            state: int(n)
            for state, n in db.execute("SELECT state, count(*) FROM nodes GROUP BY state")
        }
        scalar = lambda sql: int(db.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "events": scalar("SELECT count(*) FROM events"),
            "slots": scalar("SELECT count(*) FROM slots"),
            "fact_versions": scalar("SELECT count(*) FROM fact_versions"),
            "revision": scalar("SELECT COALESCE(max(revision), 0) FROM revisions"),
            "nodes": dict(sorted(counts.items())),
        }
