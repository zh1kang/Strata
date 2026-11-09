"""Exception hierarchy for Strata."""

from __future__ import annotations


class StrataError(Exception):
    """Base class for all Strata errors."""


class NotFoundError(StrataError, KeyError):
    """A referenced event, fact, or node does not exist."""

    def __str__(self) -> str:
        return Exception.__str__(self)


class ConflictError(StrataError):
    """A compare-and-set precondition or a duplicate-key content check failed."""


class LevelViolationError(StrataError):
    """A derivation read a node whose level is not strictly lower than its own."""


class DependencyNotReadyError(StrataError):
    """A derivation read a node that is not currently valid; retry later."""


class LeaseLostError(StrataError):
    """A rebuild lost its lease before it could commit."""


class FTSUnavailableError(StrataError):
    """The SQLite build lacks FTS5."""


class SchemaError(StrataError):
    """The database schema is missing, corrupt, or from an unsupported version."""


class ExtractionError(StrataError):
    """Model output could not be parsed or failed schema validation."""
