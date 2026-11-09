"""Structured fact extraction with strict validation.

Model output is untrusted. It is parsed, schema-checked, and (optionally)
grounded against the source event text before anything is written. Validation is
all-or-nothing: one malformed fact rejects the whole response, so a partially
understood reply never reaches memory. Invalidation never depends on anything the
model says; facts are asserted with the source event as provenance and derived
nodes track what they actually read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..errors import ExtractionError
from ..index import flatten
from ..memory import Memory
from ..store import encode
from ..types import Event, FactWrite
from .model import Model, ModelRequest

MAX_NAME_LENGTH = 200
MAX_VALUE_LENGTH = 10_000
_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.DOTALL)
_FACT_KEYS = {"subject", "attribute", "value", "evidence", "confidence"}
_SYSTEM = (
    "Extract durable facts from the event. Reply with a single JSON object only: "
    '{"facts": [{"subject": str, "attribute": str, "value": <json, not null>, '
    '"evidence": <exact quote from the event>, "confidence": <number 0..1>}]}. '
    'Use {"facts": []} when there are none.'
)


@dataclass(frozen=True)
class ExtractedFact:
    subject: str
    attribute: str
    value: Any
    evidence: str | None
    confidence: float


@dataclass(frozen=True)
class ExtractionResult:
    facts: tuple[ExtractedFact, ...]
    dropped_low_confidence: int = 0


def _reject_constant(name: str) -> Any:
    raise ExtractionError(f"model output contains non-JSON constant {name}")


_TYPOGRAPHY = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"}
)


def _normalise(text: str) -> str:
    """Compare quotes ignoring whitespace, case, and curly-versus-straight punctuation."""
    return " ".join(text.translate(_TYPOGRAPHY).replace("\u2026", "...").split()).casefold()


def event_source_text(event: Event) -> str:
    text = event.payload.get("text")
    return text if isinstance(text, str) else flatten(event.payload)


def _name(raw: Any, label: str, position: int) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ExtractionError(f"fact {position}: {label} must be a non-empty string")
    value = raw.strip()
    if len(value) > MAX_NAME_LENGTH or any(ord(c) < 32 for c in value):
        raise ExtractionError(f"fact {position}: {label} is too long or has control characters")
    return value


def parse_extraction(
    text: str,
    *,
    source_text: str | None = None,
    max_facts: int = 50,
    min_confidence: float = 0.0,
    require_evidence: bool = False,
) -> ExtractionResult:
    """Validate model output; raises :class:`ExtractionError` on any defect.

    A reply wrapped in one markdown code fence is accepted; anything else around
    the JSON object is rejected. When ``source_text`` is given, each ``evidence``
    quote must occur in it (whitespace- and case-insensitively).
    """
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        data = json.loads(body, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model output is not valid JSON: {exc.msg}") from exc
    except RecursionError as exc:
        raise ExtractionError("model output is nested too deeply") from exc
    if not isinstance(data, dict) or set(data) != {"facts"} or not isinstance(data["facts"], list):
        raise ExtractionError('model output must be an object of the form {"facts": [...]}')
    if len(data["facts"]) > max_facts:
        raise ExtractionError(f"model returned more than {max_facts} facts")

    haystack = None if source_text is None else _normalise(source_text)
    seen: dict[tuple[str, str], str] = {}
    kept: list[ExtractedFact] = []
    dropped = 0
    for position, raw in enumerate(data["facts"]):
        if not isinstance(raw, dict):
            raise ExtractionError(f"fact {position}: must be an object")
        unknown = set(raw) - _FACT_KEYS
        if unknown:
            raise ExtractionError(f"fact {position}: unknown field(s) {sorted(unknown)}")
        for required in ("subject", "attribute", "value"):
            if required not in raw:
                raise ExtractionError(f"fact {position}: missing {required}")
        subject = _name(raw["subject"], "subject", position)
        attribute = _name(raw["attribute"], "attribute", position)
        value = raw["value"]
        if value is None:
            raise ExtractionError(f"fact {position}: value must not be null")
        if len(encode(value)) > MAX_VALUE_LENGTH:
            raise ExtractionError(f"fact {position}: value is too large")
        confidence = raw.get("confidence", 1.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ExtractionError(f"fact {position}: confidence must be a number")
        if not 0.0 <= confidence <= 1.0:
            raise ExtractionError(f"fact {position}: confidence must be within [0, 1]")
        evidence = raw.get("evidence")
        if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
            raise ExtractionError(f"fact {position}: evidence must be a non-empty string")
        if evidence is None and require_evidence:
            raise ExtractionError(f"fact {position}: evidence is required")
        if evidence is not None and haystack is not None and _normalise(evidence) not in haystack:
            raise ExtractionError(f"fact {position}: evidence is not present in the source event")
        encoded = encode(value)
        if seen.setdefault((subject, attribute), encoded) != encoded:
            raise ExtractionError(
                f"fact {position}: conflicting values for {subject}.{attribute} in one response"
            )
        if confidence < min_confidence:
            dropped += 1
            continue
        fact = ExtractedFact(subject, attribute, value, evidence, float(confidence))
        if not any(f.subject == subject and f.attribute == attribute for f in kept):
            kept.append(fact)
    return ExtractionResult(tuple(kept), dropped)


def build_extraction_request(event: Event) -> ModelRequest:
    prompt = f"Event kind: {event.kind}\nEvent text:\n{event_source_text(event)}"
    return ModelRequest(prompt=prompt, system=_SYSTEM, temperature=0.0)


def extract_facts(
    model: Model,
    event: Event,
    *,
    max_facts: int = 50,
    min_confidence: float = 0.0,
    require_evidence: bool = False,
) -> ExtractionResult:
    response = model.complete(build_extraction_request(event))
    return parse_extraction(
        response.text,
        source_text=event_source_text(event),
        max_facts=max_facts,
        min_confidence=min_confidence,
        require_evidence=require_evidence,
    )


def apply_extraction(memory: Memory, event_key: str, result: ExtractionResult) -> list[int]:
    """Assert all extracted facts atomically with ``event_key`` as their source."""
    writes = [FactWrite(f.subject, f.attribute, f.value, event_key) for f in result.facts]
    return memory.assert_facts(writes) if writes else []


def extract_and_assert(
    memory: Memory,
    model: Model,
    event_key: str,
    *,
    max_facts: int = 50,
    min_confidence: float = 0.0,
    require_evidence: bool = False,
) -> ExtractionResult:
    """Extract from a stored event and assert the facts. Nothing is written on failure."""
    event = memory.event(event_key)
    result = extract_facts(
        model,
        event,
        max_facts=max_facts,
        min_confidence=min_confidence,
        require_evidence=require_evidence,
    )
    apply_extraction(memory, event_key, result)
    return result
