"""Deterministic scripted model for tests and offline development."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .model import ModelRequest, ModelResponse

Script = Mapping[str, str] | Sequence[str] | Callable[[ModelRequest], str]


class FakeModel:
    """Replies from a script.

    * a mapping: the reply for the first key (insertion order) contained in the prompt;
    * a sequence: replies returned in order, one per call;
    * a callable: ``fn(request) -> text``.

    Unmatched or exhausted scripts raise ``LookupError`` so tests fail loudly.
    """

    def __init__(self, script: Script, name: str = "fake") -> None:
        self._script = script
        self._name = name
        self._index = 0
        self.calls: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return self._name

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        script = self._script
        if callable(script):
            return ModelResponse(script(request), self._name)
        if isinstance(script, Mapping):
            for needle, reply in script.items():
                if needle in request.prompt:
                    return ModelResponse(reply, self._name)
            raise LookupError("no scripted reply matches the prompt")
        if self._index >= len(script):
            raise LookupError("scripted replies exhausted")
        reply = script[self._index]
        self._index += 1
        return ModelResponse(reply, self._name)
