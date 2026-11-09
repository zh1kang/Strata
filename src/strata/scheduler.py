"""Deterministic scheduling policies.

Both policies order *eligible* dirty nodes (those whose node dependencies are all
valid) and are total orders: ties always fall through to ``node_id``.

* ``fifo``: oldest dirty first (``dirty_since`` revision, then node id).
* ``priority``: highest ``(1 + accesses) / cost`` first, so frequently read and
  cheap nodes are repaired before rarely read or expensive ones; ties by age.

Budget handling is first-fit in policy order: a node that does not fit in the
remaining budget is skipped (and reported), and later nodes that do fit still run.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from .types import Policy


def _fifo_key(row: sqlite3.Row) -> tuple[Any, ...]:
    return (row["dirty_since"] or 0, row["node_id"])


def _priority_key(row: sqlite3.Row) -> tuple[Any, ...]:
    cost = float(row["cost"])
    weight = (1 + int(row["access_count"])) / cost if cost > 0 else math.inf
    return (-weight, row["dirty_since"] or 0, row["node_id"])


_KEYS: dict[Policy, Callable[[sqlite3.Row], tuple[Any, ...]]] = {
    Policy.FIFO: _fifo_key,
    Policy.PRIORITY: _priority_key,
}


def parse_policy(policy: Policy | str) -> Policy:
    try:
        return Policy(policy)
    except ValueError as exc:
        raise ValueError(f"unknown policy {policy!r}; expected 'fifo' or 'priority'") from exc


def order(policy: Policy, candidates: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    return sorted(candidates, key=_KEYS[policy])
