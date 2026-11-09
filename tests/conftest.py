from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from strata import Event, Memory

EVENT_KEYS = [f"e{i}" for i in range(10)]


def seed_events(mem: Memory) -> None:
    mem.ingest(
        "s",
        [Event(key, "s", "message", {"text": f"note {key} about atlas"}) for key in EVENT_KEYS],
    )


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "strata.db")


@pytest.fixture
def mem(db_path: str) -> Iterator[Memory]:
    memory = Memory.open(db_path)
    seed_events(memory)
    yield memory
    memory.close()


@pytest.fixture
def other(db_path: str, mem: Memory) -> Iterator[Memory]:
    """A second connection to the same database (simulates another worker)."""
    memory = Memory.open(db_path)
    yield memory
    memory.close()
