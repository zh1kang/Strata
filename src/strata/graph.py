"""Dependency graph: invalidation, verification, and provenance.

Invariant maintained by every writer: a node in state ``valid`` has all of its
node dependencies ``valid`` and every observed dependency revision equal to the
current one. Invalidation therefore marks direct dependents ``stale`` and all
transitive dependents ``suspect``; a suspect node is revalidated for free if its
inputs turn out to be unchanged (same-value cutoff) and recomputed otherwise.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence

from .types import Dependency

SLOT = "slot"
SUBJECT = "subject"
NODE = "node"
SESSION = "session"  # the session id is stored as the dependency subject

_NO_REVISION = -1


def _ids_json(ids: Iterable[int]) -> str:
    return json.dumps(sorted(set(ids)))


def invalidate_for_facts(
    db: sqlite3.Connection, revision: int, changed: Sequence[tuple[str, str]]
) -> set[int]:
    """Mark everything that observed a changed slot or its subject. Returns direct ids."""
    direct: set[int] = set()
    for subject, attribute in changed:
        rows = db.execute(
            "SELECT DISTINCT node_id FROM node_deps WHERE "
            "(dep_kind='slot' AND dep_subject=? AND dep_attribute=?) "
            "OR (dep_kind='subject' AND dep_subject=?)",
            (subject, attribute, subject),
        ).fetchall()
        direct.update(int(r[0]) for r in rows)
    invalidate_nodes(db, revision, direct)
    return direct


def invalidate_for_session(db: sqlite3.Connection, revision: int, session_id: str) -> set[int]:
    """Mark everything that read a session to which events were just appended."""
    rows = db.execute(
        "SELECT DISTINCT node_id FROM node_deps WHERE dep_kind='session' AND dep_subject=?",
        (session_id,),
    ).fetchall()
    direct = {int(r[0]) for r in rows}
    invalidate_nodes(db, revision, direct)
    return direct


def invalidate_nodes(db: sqlite3.Connection, revision: int, direct: Iterable[int]) -> None:
    """Mark ``direct`` stale and their transitive dependents suspect."""
    seeds = _ids_json(direct)
    if seeds == "[]":
        return
    db.execute(
        "UPDATE nodes SET invalidations = invalidations + (state='valid'), state='stale',"
        " attempts=0, last_error=NULL, dirty_since=COALESCE(dirty_since, ?) "
        "WHERE node_id IN (SELECT value FROM json_each(?))"
        " AND state IN ('valid','suspect','stale','failed')",
        (revision, seeds),
    )
    db.execute(
        "WITH RECURSIVE down(node_id) AS ("
        " SELECT node_id FROM node_deps WHERE dep_kind='node'"
        "  AND dep_node IN (SELECT value FROM json_each(?))"
        " UNION "
        " SELECT d.node_id FROM node_deps d JOIN down ON d.dep_kind='node'"
        "  AND d.dep_node = down.node_id) "
        "UPDATE nodes SET state='suspect', invalidations = invalidations + 1,"
        " dirty_since=COALESCE(dirty_since, ?) "
        "WHERE node_id IN (SELECT node_id FROM down) AND state='valid'",
        (seeds, revision),
    )


def stored_dependencies(db: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT dep_kind, dep_subject, dep_attribute, dep_node, observed_revision "
        "FROM node_deps WHERE node_id=? ORDER BY dep_kind, dep_subject, dep_attribute, dep_node",
        (node_id,),
    ).fetchall()


def current_revision(
    db: sqlite3.Connection, kind: str, subject: str, attribute: str, node: int
) -> int:
    """Current revision of an input. A non-valid node reads as ``-1`` (never matches)."""
    if kind == SLOT:
        row = db.execute(
            "SELECT h.changed_revision FROM slots s JOIN slot_heads h ON h.slot_id = s.slot_id "
            "WHERE s.subject=? AND s.attribute=?",
            (subject, attribute),
        ).fetchone()
        return int(row[0]) if row else 0
    if kind == SUBJECT:
        row = db.execute(
            "SELECT changed_revision FROM subjects WHERE subject=?", (subject,)
        ).fetchone()
        return int(row[0]) if row else 0
    if kind == SESSION:
        row = db.execute(
            "SELECT changed_revision FROM sessions WHERE session_id=?", (subject,)
        ).fetchone()
        return int(row[0]) if row else 0
    row = db.execute(
        "SELECT changed_revision, state FROM nodes WHERE node_id=?", (node,)
    ).fetchone()
    if row is None or row["state"] != "valid" or row["changed_revision"] is None:
        return _NO_REVISION
    return int(row["changed_revision"])


ObservedDep = tuple[str, str, str, int, int]  # kind, subject, attribute, node id, revision


def stored_observed(db: sqlite3.Connection, node_id: int) -> list[ObservedDep]:
    return [
        (
            r["dep_kind"],
            r["dep_subject"],
            r["dep_attribute"],
            int(r["dep_node"]),
            int(r["observed_revision"]),
        )
        for r in stored_dependencies(db, node_id)
    ]


def deps_match(db: sqlite3.Connection, deps: Iterable[ObservedDep]) -> bool:
    """True iff every observed revision still equals the current one."""
    return all(
        current_revision(db, kind, subject, attribute, node) == observed
        for kind, subject, attribute, node, observed in deps
    )


def dependencies(db: sqlite3.Connection, node_id: int) -> list[Dependency]:
    out = []
    for r in stored_dependencies(db, node_id):
        if r["dep_kind"] == SLOT:
            ref = f"{r['dep_subject']}/{r['dep_attribute']}"
        elif r["dep_kind"] in (SUBJECT, SESSION):
            ref = r["dep_subject"]
        else:
            n = db.execute(
                "SELECT kind, node_key FROM nodes WHERE node_id=?", (r["dep_node"],)
            ).fetchone()
            ref = f"{n['kind']}/{n['node_key']}"
        out.append(Dependency(r["dep_kind"], ref, int(r["observed_revision"])))
    return out


def dependents(db: sqlite3.Connection, node_id: int) -> list[tuple[str, str]]:
    rows = db.execute(
        "SELECT DISTINCT n.kind, n.node_key FROM node_deps d JOIN nodes n ON n.node_id = d.node_id"
        " WHERE d.dep_kind='node' AND d.dep_node=? ORDER BY n.kind, n.node_key",
        (node_id,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def upstream_dirty(db: sqlite3.Connection, targets: Iterable[int]) -> set[int]:
    """Targets plus every dirty node reachable through recorded node dependencies."""
    rows = db.execute(
        "WITH RECURSIVE up(node_id) AS ("
        " SELECT value FROM json_each(?)"
        " UNION "
        " SELECT d.dep_node FROM node_deps d JOIN up ON d.node_id = up.node_id"
        "  WHERE d.dep_kind='node') "
        "SELECT node_id FROM nodes WHERE node_id IN (SELECT node_id FROM up)"
        " AND state != 'valid'",
        (_ids_json(targets),),
    ).fetchall()
    return {int(r[0]) for r in rows}


def provenance(db: sqlite3.Connection, node_id: int) -> tuple[str, ...]:
    """Upstream facts, source events, and intermediate nodes the node was built from."""
    found: set[str] = set()
    seen_nodes: set[int] = set()
    todo = [node_id]

    def fact_entries(slot_row: sqlite3.Row) -> None:
        found.add(f"fact:{slot_row['subject']}/{slot_row['attribute']}@{slot_row['version_id']}")
        found.add(f"event:{slot_row['event_key']}")

    head_sql = (
        "SELECT s.subject, s.attribute, v.version_id, e.event_key FROM slots s"
        " JOIN slot_heads h ON h.slot_id = s.slot_id"
        " JOIN fact_versions v ON v.version_id = h.version_id"
        " JOIN events e ON e.seq = v.source_seq WHERE v.op='assert' AND s.subject=?"
    )
    while todo:
        current = todo.pop()
        if current in seen_nodes:
            continue
        seen_nodes.add(current)
        for dep in stored_dependencies(db, current):
            if dep["dep_kind"] == SLOT:
                for row in db.execute(
                    head_sql + " AND s.attribute=?", (dep["dep_subject"], dep["dep_attribute"])
                ):
                    fact_entries(row)
            elif dep["dep_kind"] == SUBJECT:
                for row in db.execute(head_sql, (dep["dep_subject"],)):
                    fact_entries(row)
            elif dep["dep_kind"] == SESSION:
                found.add(f"session:{dep['dep_subject']}")
            else:
                n = db.execute(
                    "SELECT kind, node_key FROM nodes WHERE node_id=?", (dep["dep_node"],)
                ).fetchone()
                found.add(f"node:{n['kind']}/{n['node_key']}")
                todo.append(int(dep["dep_node"]))
    return tuple(sorted(found))
