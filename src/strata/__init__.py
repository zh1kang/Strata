"""Strata: incrementally maintained, provenance-aware memory for agents."""

from .context import Context
from .errors import (
    ConflictError,
    DependencyNotReadyError,
    ExtractionError,
    FTSUnavailableError,
    LeaseLostError,
    LevelViolationError,
    NotFoundError,
    SchemaError,
    StrataError,
)
from .memory import Memory
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
    NodeState,
    Policy,
    RecallItem,
    RecallResult,
)

__version__ = "0.2.0"

__all__ = [
    "Budget",
    "ConflictError",
    "Context",
    "Dependency",
    "DependencyNotReadyError",
    "Event",
    "ExtractionError",
    "FTSUnavailableError",
    "Fact",
    "FactWrite",
    "IngestResult",
    "LeaseLostError",
    "LevelViolationError",
    "MaintenanceReport",
    "Memory",
    "NodeInfo",
    "NodeRead",
    "NodeState",
    "NotFoundError",
    "Policy",
    "RecallItem",
    "RecallResult",
    "SchemaError",
    "StrataError",
]
