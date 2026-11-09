"""Derivations that need no user code, so the CLI can maintain them across processes."""

from __future__ import annotations

from typing import Any

from .context import Context
from .memory import Memory

PROFILE_KIND = "profile"


def profile(ctx: Context) -> dict[str, Any]:
    """All live attributes of the subject named by the node key."""
    return ctx.subject(ctx.key)


def profile_search_text(value: Any) -> str:
    """Attribute names only: a profile holds every value of its subject, so indexing the
    values would make it match any query that one of its facts matches."""
    return " ".join(sorted(value)) if isinstance(value, dict) else ""


def define_profile(memory: Memory, subject: str, cost: float = 1.0) -> None:
    memory.define(PROFILE_KIND, subject, 1, profile, cost, search_text=profile_search_text)


def rebind_profiles(memory: Memory) -> int:
    """Re-register the derivation function for persisted ``profile`` nodes."""
    bound = 0
    for node in memory.nodes():
        if node.kind == PROFILE_KIND:
            memory.define(
                PROFILE_KIND,
                node.key,
                node.level,
                profile,
                node.cost,
                node.code_version,
                search_text=profile_search_text,
            )
            bound += 1
    return bound
