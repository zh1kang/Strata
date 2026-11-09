"""Maintenance engine: node definitions, leases, optimistic rebuilds, scheduling.

A rebuild runs the derivation *outside* any write transaction, then commits in a
single ``BEGIN IMMEDIATE`` transaction that re-checks (a) the lease token and
(b) that every input revision observed during the run still matches. If an input
moved, the work is discarded and counted as wasted; a stale result is never
committed as valid.
"""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from . import graph, index, scheduler
from .context import Context
from .errors import (
    DependencyNotReadyError,
    LeaseLostError,
    LevelViolationError,
    NotFoundError,
)
from .store import Store, digest, encode
from .types import DIRTY_STATES, Budget, MaintenanceReport, NodeInfo, Policy

_EPS = 1e-9
_DIRTY_SQL = ",".join(f"'{s.value}'" for s in DIRTY_STATES if s.value != "failed")

Derivation = Callable[[Context], Any]
SearchText = Callable[[Any], str]


@dataclass(frozen=True)
class _Registered:
    fn: Derivation
    cost: float
    version: str
    search_text: SearchText | None = None

    def index_text(self, kind: str, key: str, value: Any) -> str:
        body = self.search_text(value) if self.search_text else index.flatten(value)
        return f"{kind} {key} {body}"


class Engine:
    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], float] = time.time,
        lease_ttl: float = 300.0,
        max_attempts: int = 3,
    ) -> None:
        if lease_ttl <= 0 or max_attempts < 1:
            raise ValueError("lease_ttl must be positive and max_attempts at least 1")
        self.store = store
        self.clock = clock
        self.lease_ttl = lease_ttl
        self.max_attempts = max_attempts
        self.registry: dict[tuple[str, str], _Registered] = {}
        self.totals = MaintenanceReport()  # cumulative over every maintain() call

    # ------------------------------------------------------------------ definitions

    def define(
        self,
        kind: str,
        key: str,
        level: int,
        fn: Derivation,
        cost: float = 1.0,
        version: str = "1",
        search_text: SearchText | None = None,
    ) -> None:
        """Register a derivation and ensure its node exists.

        Changing ``version`` (i.e. the derivation's code) invalidates the node
        and its dependents. Changing ``level`` of an existing node is an error.

        A node is searchable by kind and key from the moment it is defined, so a
        query can find (and withhold or lazily build) a node that was never built.
        Once built it is indexed by ``search_text(value)``, by default the
        flattened value.
        """
        if not kind or not key:
            raise ValueError("node kind and key must be non-empty")
        if not isinstance(level, int) or level < 1:
            raise ValueError("level must be an integer >= 1")
        if math.isnan(cost) or math.isinf(cost) or cost < 0:
            raise ValueError("cost must be a finite non-negative number")
        db = self.store.db
        with self.store.write() as txn:
            row = db.execute(
                "SELECT node_id, level, code_version, state, value FROM nodes"
                " WHERE kind=? AND node_key=?",
                (kind, key),
            ).fetchone()
            if row is None:
                node_id = db.execute(
                    "INSERT INTO nodes(kind, node_key, level, cost, code_version, state,"
                    " dirty_since)"
                    " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (kind, key, level, cost, version, txn.revision),
                ).lastrowid
                index.upsert(db, "node", int(node_id or 0), f"{kind} {key}")
            else:
                if row["value"] is None:  # databases written before unbuilt nodes were indexed
                    index.upsert(db, "node", int(row["node_id"]), f"{kind} {key}")
                if row["level"] != level:
                    raise ValueError(
                        f"node {kind}/{key} already has level {row['level']}, not {level}"
                    )
                db.execute(
                    "UPDATE nodes SET cost=?, code_version=? WHERE node_id=?",
                    (cost, version, row["node_id"]),
                )
                if row["code_version"] != version and row["state"] != "pending":
                    graph.invalidate_nodes(db, txn.revision, [row["node_id"]])
        self.registry[(kind, key)] = _Registered(fn, float(cost), version, search_text)

    # ------------------------------------------------------------------ queries

    def _node_row(self, kind: str, key: str) -> sqlite3.Row:
        row = self.store.db.execute(
            "SELECT * FROM nodes WHERE kind=? AND node_key=?", (kind, key)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown node: {kind}/{key}")
        return row

    def node_id(self, kind: str, key: str) -> int:
        return int(self._node_row(kind, key)["node_id"])

    @staticmethod
    def _info(row: sqlite3.Row) -> NodeInfo:
        return NodeInfo(
            row["kind"],
            row["node_key"],
            int(row["level"]),
            row["state"],
            json.loads(row["value"]) if row["value"] is not None else None,
            float(row["cost"]),
            row["code_version"],
            row["changed_revision"],
            row["verified_revision"],
            int(row["attempts"]),
            row["last_error"],
            int(row["access_count"]),
        )

    def get_node(self, kind: str, key: str) -> NodeInfo:
        return self._info(self._node_row(kind, key))

    def list_nodes(self, state: str | None = None) -> list[NodeInfo]:
        sql = "SELECT * FROM nodes"
        params: tuple[str, ...] = ()
        if state is not None:
            sql += " WHERE state=?"
            params = (state,)
        rows = self.store.db.execute(sql + " ORDER BY level, kind, node_key", params).fetchall()
        return [self._info(r) for r in rows]

    def dirty_count(self) -> int:
        return int(
            self.store.db.execute("SELECT count(*) FROM nodes WHERE state!='valid'").fetchone()[0]
        )

    def touch(self, node_ids: Collection[int]) -> None:
        """Record reads of nodes; the priority policy weights nodes by access count."""
        if not node_ids:
            return
        with self.store.write():
            self.store.db.execute(
                "UPDATE nodes SET access_count = access_count + 1"
                " WHERE node_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(set(node_ids))),),
            )

    # ------------------------------------------------------------------ bulk state changes

    def mark_all_stale(self) -> int:
        """Force every built node dirty (used for full rebuilds). Returns count."""
        db = self.store.db
        with self.store.write() as txn:
            ids = [
                int(r[0]) for r in db.execute("SELECT node_id FROM nodes WHERE state!='pending'")
            ]
            if ids:
                graph.invalidate_nodes(db, txn.revision, ids)
        return len(ids)

    def retry_failed(self) -> int:
        """Return failed nodes to the queue with a fresh attempt budget."""
        db = self.store.db
        with self.store.write() as txn:
            ids = [int(r[0]) for r in db.execute("SELECT node_id FROM nodes WHERE state='failed'")]
            if ids:
                graph.invalidate_nodes(db, txn.revision, ids)
        return len(ids)

    # ------------------------------------------------------------------ scheduling

    def _candidates(self) -> list[sqlite3.Row]:
        return self.store.db.execute(
            "SELECT n.* FROM nodes n WHERE n.state IN (" + _DIRTY_SQL + ")"
            " AND NOT EXISTS (SELECT 1 FROM node_deps d JOIN nodes u ON u.node_id = d.dep_node"
            "  WHERE d.node_id = n.node_id AND d.dep_kind='node' AND u.state != 'valid')"
            " ORDER BY n.node_id"
        ).fetchall()

    def maintain(
        self,
        budget: Budget | float | None = None,
        policy: Policy | str = Policy.PRIORITY,
        only: Collection[int] | None = None,
    ) -> MaintenanceReport:
        """Repair dirty nodes within ``budget`` in ``policy`` order.

        ``budget=None`` is unlimited. Verification of suspect nodes whose inputs
        did not change is free and always runs. ``only`` restricts the run to
        the given node ids (used for on-demand refresh).
        """
        pol = scheduler.parse_policy(policy)
        if budget is None:
            bud = Budget.unlimited()
        elif isinstance(budget, Budget):
            bud = budget
        else:
            bud = Budget(float(budget))
        report = MaintenanceReport()
        allowed = None if only is None else set(only)
        settled: set[int] = set()  # never revisited this run (failed, conflicted, ...)
        blocked: set[int] = set()  # revisited after any progress
        skipped: set[int] = set()  # did not fit the budget
        needs_compute: set[int] = set()  # suspect nodes whose inputs changed
        attempts = 0

        while True:
            cands = [
                r
                for r in self._candidates()
                if (allowed is None or r["node_id"] in allowed) and r["node_id"] not in settled
            ]
            progressed = False
            for row in cands:
                if row["state"] == "suspect" and row["node_id"] not in needs_compute:
                    if self._verify(row["node_id"]):
                        report.verified += 1
                        report.outcomes.append(f"{row['kind']}:{row['node_key']}:verified")
                        progressed = True
                    else:
                        needs_compute.add(row["node_id"])
            if progressed:
                blocked.clear()
                continue
            if bud.max_jobs is not None and attempts >= bud.max_jobs:
                break
            chosen: sqlite3.Row | None = None
            for row in scheduler.order(pol, cands):
                node_id = row["node_id"]
                if node_id in blocked or node_id in skipped:
                    continue
                reg = self.registry.get((row["kind"], row["node_key"]))
                if reg is None:
                    settled.add(node_id)
                    report.unregistered += 1
                    report.outcomes.append(f"{row['kind']}:{row['node_key']}:unregistered")
                    continue
                if reg.cost > bud.cost - report.cost + _EPS:
                    skipped.add(node_id)
                    report.outcomes.append(f"{row['kind']}:{row['node_key']}:skipped_budget")
                    continue
                chosen = row
                break
            if chosen is None:
                break
            attempts += 1
            outcome = self._run(chosen, self.registry[(chosen["kind"], chosen["node_key"])], report)
            report.outcomes.append(f"{chosen['kind']}:{chosen['node_key']}:{outcome}")
            if outcome in ("computed", "cutoff"):
                blocked.clear()
            elif outcome == "not_ready":
                blocked.add(chosen["node_id"])
            else:
                settled.add(chosen["node_id"])

        report.skipped_budget = sum(1 for n in skipped if self._is_dirty(n))
        report.dirty = self.dirty_count()
        self.totals.merge(report)
        return report

    def _is_dirty(self, node_id: int) -> bool:
        row = self.store.db.execute(
            "SELECT state FROM nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        return row is not None and row[0] != "valid"

    # ------------------------------------------------------------------ verification

    def _verify(self, node_id: int) -> bool:
        """Revalidate a suspect node at zero cost if none of its inputs changed."""
        db = self.store.db
        with self.store.write() as txn:
            row = db.execute("SELECT state FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if row is None or row["state"] != "suspect":
                return False
            if not graph.deps_match(db, graph.stored_observed(db, node_id)):
                return False
            db.execute(
                "UPDATE nodes SET state='valid', verified_revision=?, dirty_since=NULL,"
                " attempts=0, last_error=NULL WHERE node_id=?",
                (txn.revision, node_id),
            )
        return True

    # ------------------------------------------------------------------ rebuild

    def _claim(self, node_id: int) -> str | None:
        db = self.store.db
        token = secrets.token_hex(8)
        now = self.clock()
        with self.store.write():
            state = db.execute("SELECT state FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if state is None or state["state"] == "valid":
                return None
            db.execute("DELETE FROM leases WHERE node_id=? AND expires_at<=?", (node_id, now))
            cur = db.execute(
                "INSERT OR IGNORE INTO leases(node_id, token, expires_at) VALUES (?, ?, ?)",
                (node_id, token, now + self.lease_ttl),
            )
            return token if cur.rowcount == 1 else None

    def _release(self, node_id: int, token: str) -> None:
        with self.store.write():
            self.store.db.execute(
                "DELETE FROM leases WHERE node_id=? AND token=?", (node_id, token)
            )

    def _run(self, row: sqlite3.Row, reg: _Registered, report: MaintenanceReport) -> str:
        node_id = int(row["node_id"])
        token = self._claim(node_id)
        if token is None:
            report.lease_busy += 1
            return "lease_busy"
        ctx = Context(self.store, node_id, row["kind"], row["node_key"], int(row["level"]))
        released = False
        try:
            try:
                value = reg.fn(ctx)
                ctx.raise_if_signalled()
                text = encode(value)
            except DependencyNotReadyError:
                report.not_ready += 1
                return "not_ready"
            except LevelViolationError as exc:
                self._fail(node_id, token, exc, permanent=True)
                released = True
                self._charge(report, reg.cost, wasted=True, failed=True)
                raise
            except Exception as exc:
                self._fail(node_id, token, exc, permanent=False)
                released = True
                self._charge(report, reg.cost, wasted=True, failed=True)
                return "failed"
            try:
                outcome = self._commit(row, reg, ctx, token, text)
            except LeaseLostError:
                self._charge(report, reg.cost, wasted=True, conflict=True)
                return "lease_lost"
            released = True
            if outcome == "conflict":
                self._charge(report, reg.cost, wasted=True, conflict=True)
                return "conflict"
            self._charge(report, reg.cost)
            report.jobs += 1
            if outcome == "cutoff":
                report.cutoffs += 1
            else:
                report.write_amplification += 1
            return outcome
        finally:
            if not released:
                self._release(node_id, token)

    @staticmethod
    def _charge(
        report: MaintenanceReport,
        cost: float,
        *,
        wasted: bool = False,
        conflict: bool = False,
        failed: bool = False,
    ) -> None:
        report.cost += cost
        if wasted:
            report.wasted_cost += cost
        report.conflicts += int(conflict)
        report.failures += int(failed)

    def _commit(
        self, row: sqlite3.Row, reg: _Registered, ctx: Context, token: str, text: str
    ) -> str:
        db = self.store.db
        node_id = int(row["node_id"])
        with self.store.write() as txn:
            held = db.execute("SELECT token FROM leases WHERE node_id=?", (node_id,)).fetchone()
            if held is None or held["token"] != token:
                raise LeaseLostError(f"lease for node {node_id} was lost")
            db.execute("DELETE FROM leases WHERE node_id=?", (node_id,))
            current = db.execute(
                "SELECT code_version, value_hash FROM nodes WHERE node_id=?", (node_id,)
            ).fetchone()
            observed = ctx.observed_list()
            if current["code_version"] != reg.version or not graph.deps_match(db, observed):
                return "conflict"
            new_hash = digest(text)
            changed = current["value_hash"] != new_hash
            rev = txn.revision
            db.execute("DELETE FROM node_deps WHERE node_id=?", (node_id,))
            db.executemany(
                "INSERT INTO node_deps(node_id, dep_kind, dep_subject, dep_attribute, dep_node,"
                " observed_revision) VALUES (?, ?, ?, ?, ?, ?)",
                [(node_id, k, s, a, n, o) for k, s, a, n, o in observed],
            )
            db.execute(
                "UPDATE nodes SET state='valid', value=?, value_hash=?, verified_revision=?,"
                " changed_revision=CASE WHEN ? THEN ? ELSE changed_revision END,"
                " dirty_since=NULL, attempts=0, last_error=NULL WHERE node_id=?",
                (text, new_hash, rev, changed, rev, node_id),
            )
            if changed:
                index.upsert(
                    db,
                    "node",
                    node_id,
                    reg.index_text(row["kind"], row["node_key"], json.loads(text)),
                )
            return "computed" if changed else "cutoff"

    def _fail(self, node_id: int, token: str, exc: Exception, permanent: bool) -> None:
        db = self.store.db
        with self.store.write():
            db.execute("DELETE FROM leases WHERE node_id=? AND token=?", (node_id, token))
            row = db.execute("SELECT attempts FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            attempts = int(row["attempts"]) + 1
            exhausted = permanent or attempts >= self.max_attempts
            db.execute(
                "UPDATE nodes SET attempts=?, last_error=?,"
                " state=CASE WHEN ? THEN 'failed' ELSE state END WHERE node_id=?",
                (attempts, f"{type(exc).__name__}: {exc}"[:500], exhausted, node_id),
            )
