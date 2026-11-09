"""Search quality: stemming, stopwords, fact-expanded events, snippets, time windows."""

from __future__ import annotations

import pytest

from strata import Event, FactWrite, Memory
from strata.index import match_expression, snippet


def test_stopwords_are_dropped_unless_nothing_else_remains() -> None:
    assert match_expression("What is the launch date?") == '"launch" OR "date"'
    assert match_expression("what is it") == '"what" OR "is" OR "it"'
    assert match_expression("  ?! ") is None


def test_stemming_matches_inflected_words(mem: Memory) -> None:
    mem.ingest("t", [Event("t1", "t", "message", {"text": "We launched the rockets yesterday"})])
    assert [i.key for i in mem.search_events("when did the rocket launch")] == ["t1"]


def test_an_event_is_found_by_the_facts_extracted_from_it(mem: Memory) -> None:
    mem.ingest("t", [Event("t1", "t", "message", {"text": "Finally got a puppy, named him Rex"})])
    assert mem.search_events("dog") == []
    mem.assert_fact("user", "pet_dog", "Rex, a puppy", "t1")
    assert [i.key for i in mem.search_events("what is my dog called")] == ["t1"]
    # the raw item still shows what was said, not the extracted fact
    assert mem.search_events("dog")[0].value == {"text": "Finally got a puppy, named him Rex"}


def test_snippet_keeps_short_text_and_centres_long_text_on_matches() -> None:
    assert snippet("short text", "anything", 50) == "short text"
    filler = " ".join(f"Sentence {i} is about nothing in particular." for i in range(40))
    text = f"{filler} My dentist appointment moved to Friday. {filler}"
    cut = snippet(text, "when is the dentist appointment", 200)
    assert len(cut) <= 200
    assert "My dentist appointment moved to Friday." in cut
    assert cut.startswith("…") and cut.endswith("…")
    head = snippet(text, "zebra", 60)
    assert head.startswith("Sentence 0") and len(head) <= 60
    long_sentence = "x " * 300 + "the dentist is here " + "y " * 300
    window = snippet(long_sentence, "dentist", 80)
    assert "dentist" in window and len(window) <= 80
    with pytest.raises(ValueError):
        snippet(text, "x", 0)


def dated(mem: Memory) -> None:
    mem.ingest(
        "d",
        [
            Event("may", "d", "message", {"text": "bought a bike"}, "2023-05-20T10:00"),
            Event("june", "d", "message", {"text": "sold the bike"}, "2023-06-02"),
            Event("undated", "d", "message", {"text": "bike talk"}),
        ],
    )


def test_event_times_are_normalised_stored_and_part_of_identity(mem: Memory) -> None:
    dated(mem)
    assert mem.event("may").at == "2023-05-20T10:00:00"
    assert mem.event("june").at == "2023-06-02T00:00:00"
    assert mem.event("undated").at is None
    again = mem.ingest(
        "d", [Event("may", "d", "message", {"text": "bought a bike"}, "2023-05-20 10:00")]
    )
    assert again.duplicates == 1
    with pytest.raises(Exception, match="different content"):
        mem.ingest("d", [Event("may", "d", "message", {"text": "bought a bike"}, "2023-05-21")])
    with pytest.raises(ValueError, match="ISO 8601"):
        mem.ingest("d", [Event("bad", "d", "message", {"text": "x"}, "May 20th")])


def test_since_and_until_filter_events_and_facts(mem: Memory) -> None:
    dated(mem)
    mem.assert_facts(
        [FactWrite("user", "bike", "bought", "may"), FactWrite("user", "bike_sold", True, "june")]
    )
    assert {i.key for i in mem.search_events("bike")} == {"may", "june", "undated"}
    assert [i.key for i in mem.search_events("bike", since="2023-06-01")] == ["june"]
    assert [i.key for i in mem.search_events("bike", until="2023-06-01")] == ["may"]

    result = mem.recall("bike", events=5, until="2023-06-01")
    by_kind = {(i.kind, i.key): i for i in result.items}
    assert ("fact", "user/bike") in by_kind and ("fact", "user/bike_sold") not in by_kind
    assert by_kind[("fact", "user/bike")].at == "2023-05-20T10:00:00"
    assert {k for kind, k in by_kind if kind == "event"} == {"may"}
    assert mem.get_fact("user", "bike_sold").at == "2023-06-02T00:00:00"
