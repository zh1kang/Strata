"""Derivations that need no user code, so the CLI can maintain them across processes."""

from __future__ import annotations

from typing import Any

from .context import Context
from .memory import Memory

PROFILE_KIND = "profile"
TOPIC_KIND = "topic"
TOPIC_PREFIX = "_topic:"  # internal subject of a topic's membership slot
TOPIC_MEMBERS = "members"  # set-valued slot of [subject, attribute] pairs


def profile(ctx: Context) -> dict[str, Any]:
    """All live attributes of the subject named by the node key."""
    return ctx.subject(ctx.key)


def profile_search_text(value: Any) -> str:
    """Attribute names only: a profile holds every value of its subject, so indexing the
    values would make it match any query that one of its facts matches."""
    return " ".join(sorted(value)) if isinstance(value, dict) else ""


def define_profile(memory: Memory, subject: str, cost: float = 1.0) -> None:
    memory.define(PROFILE_KIND, subject, 1, profile, cost, search_text=profile_search_text)


def topic_subject(topic: str) -> str:
    return TOPIC_PREFIX + topic.casefold()


def topic(ctx: Context) -> dict[str, Any]:
    """Live facts filed under the topic named by the node key, as ``{"subject.attribute": value}``.

    Reads the topic's membership slot and then each member slot, so the node is
    invalidated when a member changes or a new fact joins the topic, and by
    nothing else.
    """
    members = ctx.slot(topic_subject(ctx.key), TOPIC_MEMBERS, [])
    facts: dict[str, Any] = {}
    for member in members if isinstance(members, list) else []:
        if isinstance(member, list) and len(member) == 2:
            value = ctx.slot(str(member[0]), str(member[1]))
            if value is not None:
                facts[f"{member[0]}.{member[1]}"] = value
    return facts


def topic_search_text(value: Any) -> str:
    """Attribute names only, for the same reason as :func:`profile_search_text`."""
    if not isinstance(value, dict):
        return ""
    return " ".join(sorted({name.rsplit(".", 1)[-1] for name in value}))


def define_topic(memory: Memory, name: str, cost: float = 1.0, keywords: str = "") -> None:
    memory.define(
        TOPIC_KIND,
        name.casefold(),
        1,
        topic,
        cost,
        search_text=topic_search_text,
        keywords=keywords,
    )


_BUILTIN = {
    PROFILE_KIND: (profile, profile_search_text),
    TOPIC_KIND: (topic, topic_search_text),
}


def rebind_profiles(memory: Memory) -> int:
    """Re-register the derivation functions of persisted ``profile`` and ``topic`` nodes."""
    bound = 0
    for node in memory.nodes():
        if node.kind in _BUILTIN:
            fn, search_text = _BUILTIN[node.kind]
            memory.define(
                node.kind,
                node.key,
                node.level,
                fn,
                node.cost,
                node.code_version,
                search_text=search_text,
                keywords=node.keywords,
            )
            bound += 1
    return bound
