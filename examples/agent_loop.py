"""A minimal agent memory loop with Strata, runnable offline.

Each user message is stored as an event, a model extracts facts from it (validated against the
message text), a derived profile of the subject is kept up to date within a budget, and the agent
answers from ``recall``. When a fact changes, the old profile is withheld until it is rebuilt.

The model here is scripted so the example needs no network. To use a real provider, pass any
object with a ``name`` property and ``complete(ModelRequest) -> ModelResponse``, for example:

    class MyModel:
        name = "my-provider/my-model"

        def complete(self, request: ModelRequest) -> ModelResponse:
            text = my_client.generate(system=request.system, prompt=request.prompt)
            return ModelResponse(text, self.name)

Wrap it in ``CachedModel(model, SqliteCache("cache.db"))`` to make runs repeatable.

Run: PYTHONPATH=src python examples/agent_loop.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from strata import Budget, Event, Memory
from strata.llm import FakeModel, Model, ModelRequest, extract_and_assert
from strata.profiles import define_profile

MESSAGES = [
    ("s1", "Atlas launches on December 10."),
    ("s2", "Update: Atlas moved to January 15 because of a supplier delay."),
]


def scripted_extractor(request: ModelRequest) -> str:
    """Stands in for an extraction model: one launch-date fact per message."""
    date = "2027-01-15" if "January 15" in request.prompt else "2026-12-10"
    quote = "January 15" if "January 15" in request.prompt else "December 10"
    fact = {
        "subject": "atlas",
        "attribute": "launch_date",
        "value": date,
        "op": "set",
        "topic": "launches",
        "evidence": quote,
        "confidence": 0.95,
    }
    return json.dumps({"facts": [fact]})


def answer(memory: Memory, question: str) -> str:
    """Answer from current memory only; say so when the derived profile is being rebuilt."""
    result = memory.recall(question, limit=5)
    current = [i for i in result.items if i.status == "current"]
    lines = [f"  [{i.kind}] {i.text}" for i in current]
    if result.suppressed:
        lines.append(f"  ({result.suppressed} derived item(s) withheld until rebuilt)")
    return "\n".join(lines) or "  (nothing current)"


def run(extractor: Model) -> None:
    with tempfile.TemporaryDirectory() as tmp, Memory.open(str(Path(tmp) / "agent.db")) as memory:
        define_profile(memory, "atlas")
        for number, (session, text) in enumerate(MESSAGES, 1):
            key = f"{session}:1"
            memory.ingest(session, [Event(key, session, "message", {"text": text})])
            extract_and_assert(memory, extractor, key, require_evidence=True)
            print(f"\nUser: {text}")
            print("Agent memory before maintenance:")
            print(answer(memory, "When does Atlas launch?"))
            report = memory.maintain(Budget(10))
            print(f"Maintenance: {report.jobs} rebuilt, cost {report.cost:g}")
            print("Agent memory after maintenance:")
            print(answer(memory, "When does Atlas launch?"))
            if number == len(MESSAGES):
                history = memory.fact_history("atlas", "launch_date")
                print(f"\nHistory of atlas.launch_date: {[v.value for v in history]}")


if __name__ == "__main__":
    run(FakeModel(scripted_extractor, name="scripted-extractor"))
