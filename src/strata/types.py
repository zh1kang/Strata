"""Public value types."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeState(StrEnum):
    """Lifecycle of a derived node.

    ``valid`` is the only state whose value may be served as current.
    """

    PENDING = "pending"  # never built
    VALID = "valid"
    STALE = "stale"  # a direct input changed
    SUSPECT = "suspect"  # an upstream node may have changed; verify before use
    FAILED = "failed"  # retries exhausted

    @property
    def dirty(self) -> bool:
        return self is not NodeState.VALID


DIRTY_STATES = tuple(s for s in NodeState if s.dirty)


class Policy(StrEnum):
    FIFO = "fifo"
    PRIORITY = "priority"
    DEMAND = "demand"


@dataclass(frozen=True)
class Event:
    """One immutable observation. ``at`` is when it happened (ISO 8601), if known."""

    key: str
    session_id: str
    kind: str
    payload: dict[str, Any]
    at: str | None = None


@dataclass(frozen=True)
class IngestResult:
    inserted: int
    duplicates: int


@dataclass(frozen=True)
class FactWrite:
    """One fact assertion for :meth:`Memory.assert_facts`.

    ``expected_version`` is a compare-and-set guard on the slot head:
    ``None`` skips the check, ``0`` requires that the slot has no versions yet,
    and any other value must equal the current head version id.

    ``mode`` says how ``value`` combines with the current one:

    * ``"set"`` replaces it.
    * ``"add"`` treats the slot as a set: the new value is the current list plus
      the given item(s) not already in it (a scalar current value counts as a
      one-item list). Adding items already present changes nothing.
    * ``"remove"`` deletes the given item(s) from the current list; on a missing
      or retracted slot it writes nothing.
    * ``"retract"`` retracts the slot (``value`` is ignored); on a missing or
      already retracted slot it writes nothing.
    * ``"contingent"`` writes nothing to the slot yet. It records what the slot
      becomes once a premise changes: when any of ``premises`` later moves away
      from the value it had when the contingency was written, the slot is set to
      ``value`` in that same transaction (sourced to the event that changed the
      premise) and the contingency is used up. Requires ``premises``.

    ``premises`` names slots, as ``(subject, attribute)`` pairs, that this fact
    assumes. The value each premise has when the batch commits is recorded with
    the new version; once a premise has another value, or is itself unverified,
    the fact is served as ``unverified`` instead of ``current``.

    ``add`` and ``remove`` read the head inside the write transaction, so
    concurrent additions to one slot never lose an item. A write that writes
    nothing reports the current head version id (``0`` if there is none).
    """

    subject: str
    attribute: str
    value: Any
    source_key: str
    expected_version: int | None = None
    mode: str = "set"
    premises: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Fact:
    subject: str
    attribute: str
    value: Any
    version_id: int
    op: str  # "assert" | "retract"
    source_event: str
    revision: int
    at: str | None = None  # when the source event happened, if known


@dataclass(frozen=True)
class Budget:
    """Maintenance budget in cost units.

    A node costs what its derivation charged on its last run (``Context.charge``),
    or its declared ``cost`` if it never charged. A measured cost is only known
    once a run finishes, so a run can overshoot by one job's misestimate.
    """

    cost: float
    max_jobs: int | None = None

    def __post_init__(self) -> None:
        if math.isnan(self.cost) or self.cost < 0:
            raise ValueError("budget cost must be a non-negative number")
        if self.max_jobs is not None and self.max_jobs < 0:
            raise ValueError("max_jobs must be non-negative")

    @classmethod
    def unlimited(cls) -> Budget:
        return cls(math.inf)


@dataclass(frozen=True)
class RecallItem:
    text: str
    status: str  # "current" | "stale" | "unverified" | "raw"
    kind: str  # "fact", "event", or a node kind
    key: str
    score: float
    provenance: tuple[str, ...] = ()
    value: Any = None
    at: str | None = None  # date of the event (or of a fact's source event), if known


@dataclass(frozen=True)
class RecallResult:
    items: tuple[RecallItem, ...]
    fallback_used: bool = False
    suppressed: int = 0  # dirty derived items withheld from the current results
    refreshed: int = 0  # nodes rebuilt on demand
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeRead:
    """Result of :meth:`Memory.read_node`. ``value`` is only set when ``status`` is current."""

    kind: str
    key: str
    status: str  # "current" | "unavailable"
    state: str
    value: Any = None
    refreshed: bool = False


@dataclass(frozen=True)
class NodeInfo:
    kind: str
    key: str
    level: int
    state: str
    value: Any
    cost: float
    code_version: str
    changed_revision: int | None
    verified_revision: int | None
    attempts: int
    last_error: str | None
    access_count: int
    measured_cost: float | None = None  # last cost charged with ``Context.charge``
    keywords: str = ""  # static search text given to ``define``
    invalidations: int = 0  # times a valid value of the node was invalidated


@dataclass(frozen=True)
class FactStatus:
    """Whether a live fact may be served as current.

    ``reasons`` explains an ``unverified`` status, one entry per premise that
    changed (``"<premise> changed from <old> to <new>"``) or is itself unverified.
    """

    status: str  # "current" | "unverified"
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dependency:
    kind: str  # "slot" | "subject" | "node" | "session"
    ref: str
    observed_revision: int


@dataclass
class MaintenanceReport:
    """What one maintenance run did.

    ``cost`` is every unit spent (computed, conflicted, or failed attempts),
    ``wasted_cost`` the part that produced no commit. ``write_amplification`` is
    the number of node values actually rewritten.
    """

    jobs: int = 0  # successful recomputations
    verified: int = 0  # suspect nodes revalidated at zero cost
    cutoffs: int = 0  # recomputations whose output was unchanged
    cost: float = 0.0
    wasted_cost: float = 0.0
    conflicts: int = 0
    failures: int = 0
    lease_busy: int = 0
    not_ready: int = 0
    unregistered: int = 0
    skipped_budget: int = 0
    dirty: int = 0
    write_amplification: int = 0
    outcomes: list[str] = field(default_factory=list)

    def merge(self, other: MaintenanceReport) -> None:
        for name in (
            "jobs",
            "verified",
            "cutoffs",
            "cost",
            "wasted_cost",
            "conflicts",
            "failures",
            "lease_busy",
            "not_ready",
            "unregistered",
            "skipped_budget",
            "write_amplification",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.dirty = other.dirty
        self.outcomes.extend(other.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "jobs": self.jobs,
            "verified": self.verified,
            "cutoffs": self.cutoffs,
            "cost": self.cost,
            "wasted_cost": self.wasted_cost,
            "conflicts": self.conflicts,
            "failures": self.failures,
            "lease_busy": self.lease_busy,
            "not_ready": self.not_ready,
            "unregistered": self.unregistered,
            "skipped_budget": self.skipped_budget,
            "dirty": self.dirty,
            "write_amplification": self.write_amplification,
            "outcomes": list(self.outcomes),
        }
