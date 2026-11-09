"""Events, fact slots, CAS, and storage integrity."""

from __future__ import annotations

import sqlite3

import pytest

from strata import (
    ConflictError,
    Event,
    FactWrite,
    Memory,
    NotFoundError,
    SchemaError,
)


def revision(mem: Memory) -> int:
    return mem.status()["revision"]


def test_duplicate_events_are_idempotent(mem: Memory) -> None:
    before = (mem.status()["events"], revision(mem))
    result = mem.ingest("s", [Event("e0", "s", "message", {"text": "note e0 about atlas"})])
    assert (result.inserted, result.duplicates) == (0, 1)
    assert (mem.status()["events"], revision(mem)) == before  # no-op burns no revision


def test_same_key_different_content_is_rejected(mem: Memory) -> None:
    with pytest.raises(ConflictError):
        mem.ingest("s", [Event("e0", "s", "message", {"text": "something else"})])
    with pytest.raises(ConflictError):
        mem.ingest("s", [Event("n1", "s", "m", {"a": 1}), Event("n1", "s", "m", {"a": 2})])
    with pytest.raises(NotFoundError):
        mem.event("n1")  # the failed batch was not partially applied


def test_ingest_validation(mem: Memory) -> None:
    with pytest.raises(ValueError):
        mem.ingest("s", [Event("", "s", "m", {})])
    with pytest.raises(ValueError):
        mem.ingest("s", [Event("x", "other", "m", {})])
    with pytest.raises(ValueError):
        mem.ingest("s", [Event("x", "s", "m", {"v": float("nan")})])


def test_events_are_append_only(mem: Memory) -> None:
    with pytest.raises(sqlite3.DatabaseError):
        mem.store.db.execute("UPDATE events SET kind='x'")
    with pytest.raises(sqlite3.DatabaseError):
        mem.store.db.execute("DELETE FROM events")
    mem.assert_fact("a", "b", 1, "e0")
    with pytest.raises(sqlite3.DatabaseError):
        mem.store.db.execute("DELETE FROM fact_versions")


def test_cas_conflicts(mem: Memory) -> None:
    v1 = mem.assert_fact("a", "x", 1, "e0", expected_version=0)  # 0 = must not exist
    with pytest.raises(ConflictError):
        mem.assert_fact("a", "x", 2, "e1", expected_version=0)
    with pytest.raises(ConflictError):
        mem.assert_fact("a", "x", 2, "e1", expected_version=v1 + 1)
    assert mem.get_fact("a", "x").value == 1
    v2 = mem.assert_fact("a", "x", 2, "e1", expected_version=v1)
    assert mem.head_version("a", "x") == v2
    with pytest.raises(ConflictError):  # stale writer holding the old version loses
        mem.assert_fact("a", "x", 3, "e2", expected_version=v1)
    with pytest.raises(ConflictError):
        mem.retract_fact("a", "x", "e2", expected_version=v1)
    assert [f.value for f in mem.fact_history("a", "x")] == [1, 2]


def test_cas_conflict_between_two_connections(mem: Memory, other: Memory) -> None:
    v = mem.assert_fact("a", "x", 1, "e0")
    other.assert_fact("a", "x", 2, "e1", expected_version=v)
    with pytest.raises(ConflictError):
        mem.assert_fact("a", "x", 3, "e2", expected_version=v)
    assert mem.get_fact("a", "x").value == 2


def test_batch_is_atomic(mem: Memory) -> None:
    mem.assert_fact("a", "x", 1, "e0")
    writes = [
        FactWrite("a", "y", 5, "e1"),
        FactWrite("a", "x", 9, "e1", expected_version=999),
    ]
    with pytest.raises(ConflictError):
        mem.assert_facts(writes)
    assert mem.get_fact("a", "y") is None
    assert mem.get_fact("a", "x").value == 1


def test_unknown_source_and_unserializable_values(mem: Memory) -> None:
    with pytest.raises(NotFoundError):
        mem.assert_fact("a", "x", 1, "missing")
    with pytest.raises(NotFoundError):
        mem.retract_fact("a", "never", "e0")
    with pytest.raises(ValueError):
        mem.assert_fact("a", "x", {1, 2}, "e0")
    with pytest.raises(ValueError):
        mem.assert_fact("a", "x", float("inf"), "e0")
    with pytest.raises(ValueError):
        mem.assert_fact("", "x", 1, "e0")
    assert mem.status()["slots"] == 0


def test_retract_is_idempotent_and_reassert_revives(mem: Memory) -> None:
    mem.assert_fact("a", "x", 1, "e0")
    r1 = mem.retract_fact("a", "x", "e1")
    assert mem.retract_fact("a", "x", "e2") == r1
    assert mem.get_fact("a", "x") is None
    mem.assert_fact("a", "x", 1, "e3")
    assert mem.get_fact("a", "x").value == 1
    assert len(mem.fact_history("a", "x")) == 3


def test_falsy_and_structured_values_round_trip(mem: Memory) -> None:
    for i, value in enumerate([0, False, "", [], {}, {"b": 1, "a": [1, 2]}, None]):
        mem.assert_fact("a", f"k{i}", value, "e0")
        assert mem.get_fact("a", f"k{i}").value == value
    assert [f.attribute for f in mem.facts("a")][:2] == ["k0", "k1"]


def test_schema_version_and_foreign_database(tmp_path) -> None:
    path = str(tmp_path / "x.db")
    Memory.open(path).close()
    raw = sqlite3.connect(path)
    raw.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    raw.commit()
    raw.close()
    with pytest.raises(SchemaError):
        Memory.open(path)

    foreign = str(tmp_path / "foreign.db")
    raw = sqlite3.connect(foreign)
    raw.execute("CREATE TABLE t(x)")
    raw.commit()
    raw.close()
    with pytest.raises(SchemaError):
        Memory.open(foreign)


def test_reopen_preserves_state(db_path: str) -> None:
    with Memory.open(db_path) as m:
        m.ingest("s", [Event("e0", "s", "m", {"text": "hello"})])
        m.assert_fact("a", "x", 1, "e0")
    with Memory.open(db_path) as m:
        assert m.get_fact("a", "x").value == 1
        assert m.event("e0").payload == {"text": "hello"}
