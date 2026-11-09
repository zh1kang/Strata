"""Response caching for models. Only deterministic (temperature 0) calls are cached."""

from __future__ import annotations

import sqlite3
from typing import Protocol

from .model import Model, ModelRequest, ModelResponse


class ResponseCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, text: str) -> None: ...


class InMemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, text: str) -> None:
        self._data[key] = text

    def __len__(self) -> int:
        return len(self._data)


class SqliteCache:
    """Persistent cache in its own SQLite file, independent of the memory database."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS response_cache (key TEXT PRIMARY KEY, text TEXT NOT NULL)"
        )

    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT text FROM response_cache WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def put(self, key: str, text: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO response_cache(key, text) VALUES (?, ?)", (key, text)
        )

    def close(self) -> None:
        self._db.close()


class CachedModel:
    """Wraps a :class:`Model`, serving repeated deterministic requests from a cache."""

    def __init__(self, model: Model, cache: ResponseCache) -> None:
        self._model = model
        self._cache = cache
        self.hits = 0
        self.misses = 0

    @property
    def name(self) -> str:
        return self._model.name

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.temperature != 0:
            self.misses += 1
            return self._model.complete(request)
        key = request.cache_key(self._model.name)
        text = self._cache.get(key)
        if text is not None:
            self.hits += 1
            return ModelResponse(text, self._model.name, cached=True)
        self.misses += 1
        response = self._model.complete(request)
        self._cache.put(key, response.text)
        return ModelResponse(response.text, response.model, cached=False)
