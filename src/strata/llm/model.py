"""The provider-neutral model boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..store import digest, encode


@dataclass(frozen=True)
class ModelRequest:
    prompt: str
    system: str = ""
    temperature: float = 0.0
    max_tokens: int | None = None

    def cache_key(self, model_name: str) -> str:
        """Stable key over everything that can change the completion."""
        return digest(
            encode(
                {
                    "model": model_name,
                    "system": self.system,
                    "prompt": self.prompt,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                }
            )
        )


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    cached: bool = False


@runtime_checkable
class Model(Protocol):
    """Anything that turns a request into text.

    Adapters for real providers implement this and nothing else. They should
    raise on transport errors rather than returning partial text.
    """

    @property
    def name(self) -> str: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...
