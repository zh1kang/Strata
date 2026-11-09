"""LLM boundary: Model protocol, caching, and strict validation of model output."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from strata import Event, ExtractionError, Memory
from strata.llm import (
    CachedModel,
    FakeModel,
    InMemoryCache,
    Model,
    ModelRequest,
    SqliteCache,
    extract_and_assert,
    extract_facts,
    parse_extraction,
)

SOURCE = "Atlas now launches on Jan 15, owned by Ana."


def fact(**over: object) -> dict[str, object]:
    base = {
        "subject": "atlas",
        "attribute": "launch",
        "value": "2027-01-15",
        "evidence": "launches on Jan 15",
        "confidence": 0.9,
    }
    return {**base, **over}


def reply(*facts: dict[str, object]) -> str:
    return json.dumps({"facts": list(facts)})


# ---------------------------------------------------------------- parsing


def test_valid_output_is_parsed() -> None:
    result = parse_extraction(
        reply(fact(), fact(attribute="owner", value="ana", evidence=None)), source_text=SOURCE
    )
    assert [(f.subject, f.attribute, f.value) for f in result.facts] == [
        ("atlas", "launch", "2027-01-15"),
        ("atlas", "owner", "ana"),
    ]
    assert result.facts[0].confidence == 0.9


def test_single_code_fence_is_tolerated_but_other_wrapping_is_not() -> None:
    fenced = "```json\n" + reply(fact()) + "\n```"
    assert len(parse_extraction(fenced).facts) == 1
    with pytest.raises(ExtractionError):
        parse_extraction("Here you go: " + reply(fact()))
    with pytest.raises(ExtractionError):
        parse_extraction(reply(fact()) + "\nHope that helps!")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '{"facts": [',  # truncated
        "[]",
        '{"facts": {}}',
        '{"facts": [], "extra": 1}',
        '{"fact": []}',
        '{"facts": ["x"]}',
        '{"facts": [{"subject": "a", "attribute": "b"}]}',  # missing value
        '{"facts": [{"subject": "a", "attribute": "b", "value": null}]}',
        '{"facts": [{"subject": "", "attribute": "b", "value": 1}]}',
        '{"facts": [{"subject": 5, "attribute": "b", "value": 1}]}',
        '{"facts": [{"subject": "a", "attribute": "b", "value": 1, "extra": 1}]}',
        '{"facts": [{"subject": "a", "attribute": "b", "value": NaN}]}',
        '{"facts": [{"subject": "a", "attribute": "b", "value": 1, "confidence": 1.5}]}',
        '{"facts": [{"subject": "a", "attribute": "b", "value": 1, "confidence": -0.1}]}',
        '{"facts": [{"subject": "a", "attribute": "b", "value": 1, "confidence": "high"}]}',
        '{"facts": [{"subject": "a", "attribute": "b", "value": 1, "confidence": true}]}',
        '{"facts": [{"subject": "a", "attribute": "b", "value": 1, "evidence": 3}]}',
        '{"facts": [{"subject": "a\\nb", "attribute": "b", "value": 1}]}',
        "[" * 100_000,
    ],
)
def test_malformed_output_is_rejected(text: str) -> None:
    with pytest.raises(ExtractionError):
        parse_extraction(text)


def test_ungrounded_evidence_is_rejected() -> None:
    with pytest.raises(ExtractionError, match="not present"):
        parse_extraction(reply(fact(evidence="launches on March 3")), source_text=SOURCE)
    parse_extraction(reply(fact(evidence="LAUNCHES   on jan 15")), source_text=SOURCE)  # normalised


def test_evidence_ignores_curly_versus_straight_punctuation() -> None:
    source = 'I\u2019m moving \u2014 it\u2019s "final"\u2026'
    parse_extraction(
        reply(fact(evidence="I'm moving - it's \u201cfinal\u201d...")), source_text=source
    )


def test_limits_and_conflicts() -> None:
    with pytest.raises(ExtractionError, match="more than"):
        parse_extraction(reply(fact(), fact(attribute="b")), max_facts=1)
    with pytest.raises(ExtractionError, match="conflicting"):
        parse_extraction(reply(fact(value="a"), fact(value="b")))
    with pytest.raises(ExtractionError, match="required"):
        parse_extraction(reply(fact(evidence=None)), require_evidence=True)
    with pytest.raises(ExtractionError, match="too large"):
        parse_extraction(reply(fact(value="x" * 20_000, evidence=None)))
    assert len(parse_extraction(reply(fact(), fact())).facts) == 1  # identical duplicates collapse


def test_low_confidence_facts_are_dropped_not_errors() -> None:
    result = parse_extraction(
        reply(fact(confidence=0.2), fact(attribute="o", confidence=0.95)), min_confidence=0.5
    )
    assert [f.attribute for f in result.facts] == ["o"]
    assert result.dropped_low_confidence == 1


# ---------------------------------------------------------------- model boundary


def test_fake_model_satisfies_protocol_and_scripts() -> None:
    by_prompt = FakeModel({"alpha": "A", "beta": "B"})
    assert isinstance(by_prompt, Model)
    assert by_prompt.complete(ModelRequest("about beta")).text == "B"
    with pytest.raises(LookupError):
        by_prompt.complete(ModelRequest("gamma"))
    ordered = FakeModel(["one", "two"])
    assert [ordered.complete(ModelRequest("x")).text for _ in range(2)] == ["one", "two"]
    with pytest.raises(LookupError):
        ordered.complete(ModelRequest("x"))
    assert FakeModel(lambda r: r.prompt.upper()).complete(ModelRequest("hi")).text == "HI"


def test_response_cache_hits_and_keys() -> None:
    inner = FakeModel(["first", "second"])
    cached = CachedModel(inner, InMemoryCache())
    request = ModelRequest("same prompt")
    a, b = cached.complete(request), cached.complete(request)
    assert (a.text, a.cached, b.text, b.cached) == ("first", False, "first", True)
    assert len(inner.calls) == 1 and (cached.hits, cached.misses) == (1, 1)
    assert cached.complete(ModelRequest("same prompt", system="s")).text == "second"
    assert request.cache_key("m1") != request.cache_key("m2")
    assert request.cache_key("m1") == ModelRequest("same prompt").cache_key("m1")


def test_nonzero_temperature_is_not_cached() -> None:
    inner = FakeModel(["x", "y"])
    cached = CachedModel(inner, InMemoryCache())
    hot = ModelRequest("p", temperature=0.7)
    assert [cached.complete(hot).text for _ in range(2)] == ["x", "y"]


def test_sqlite_cache_persists(tmp_path: Path) -> None:
    path = str(tmp_path / "cache.db")
    cache = SqliteCache(path)
    CachedModel(FakeModel(["stored"]), cache).complete(ModelRequest("p"))
    cache.close()
    fresh = FakeModel([])  # would raise if called
    hit = CachedModel(fresh, SqliteCache(path)).complete(ModelRequest("p"))
    assert hit.text == "stored" and hit.cached and not fresh.calls


# ---------------------------------------------------------------- integration


def ingest_source(mem: Memory) -> Event:
    event = Event("src1", "s", "message", {"text": SOURCE})
    mem.ingest("s", [event])
    return event


def test_extract_and_assert_writes_facts_with_event_provenance(mem: Memory) -> None:
    ingest_source(mem)
    model = FakeModel([reply(fact(), fact(attribute="owner", value="Ana", evidence="Ana"))])
    result = extract_and_assert(mem, model, "src1", require_evidence=True)
    assert len(result.facts) == 2
    stored = mem.get_fact("atlas", "launch")
    assert stored.value == "2027-01-15" and stored.source_event == "src1"
    assert "Jan 15" in model.calls[0].prompt and model.calls[0].temperature == 0.0


def test_malformed_model_output_writes_nothing(mem: Memory) -> None:
    ingest_source(mem)
    before = mem.status()
    bad_replies = [
        "I think the launch is in January.",
        reply(fact(), fact(attribute="owner", value=None)),  # one bad fact poisons the batch
        reply(fact(), fact(attribute="owner", value="Zed", evidence="owned by Zed")),
    ]
    for text in bad_replies:
        with pytest.raises(ExtractionError):
            extract_and_assert(mem, FakeModel([text]), "src1")
        assert mem.status() == before


def test_extract_facts_needs_no_memory_and_cache_makes_it_repeatable() -> None:
    event = Event("k", "s", "message", {"text": SOURCE})
    inner = FakeModel([reply(fact())])
    model = CachedModel(inner, InMemoryCache())
    first = extract_facts(model, event)
    second = extract_facts(model, event)
    assert first == second and len(inner.calls) == 1


def test_provider_errors_propagate_and_write_nothing(mem: Memory) -> None:
    ingest_source(mem)

    class Boom:
        name = "boom"

        def complete(self, request: ModelRequest):
            raise TimeoutError("provider down")

    with pytest.raises(TimeoutError):
        extract_and_assert(mem, Boom(), "src1")
    assert mem.status()["slots"] == 0


def test_core_does_not_import_llm_package() -> None:
    code = "import sys, strata; assert not any(m.startswith('strata.llm') for m in sys.modules)"
    env = {"PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
    assert subprocess.run([sys.executable, "-c", code], env=env, check=False).returncode == 0
