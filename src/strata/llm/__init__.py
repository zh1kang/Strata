"""LLM integration boundary.

The Strata core never imports this package. A real provider adapter only needs
to implement :class:`Model`; everything else (caching, strict output validation,
asserting facts with event provenance) is provider-independent.
"""

from .cache import CachedModel, InMemoryCache, ResponseCache, SqliteCache
from .extraction import (
    ExtractedFact,
    ExtractionResult,
    apply_extraction,
    build_extraction_request,
    extract_and_assert,
    extract_facts,
    parse_extraction,
)
from .fake import FakeModel
from .model import Model, ModelRequest, ModelResponse

__all__ = [
    "CachedModel",
    "ExtractedFact",
    "ExtractionResult",
    "FakeModel",
    "InMemoryCache",
    "Model",
    "ModelRequest",
    "ModelResponse",
    "ResponseCache",
    "SqliteCache",
    "apply_extraction",
    "build_extraction_request",
    "extract_and_assert",
    "extract_facts",
    "parse_extraction",
]
