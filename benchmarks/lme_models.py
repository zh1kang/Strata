"""Model backends and a persistent, thread-safe response cache for the LongMemEval benchmark.

Two kinds of backend are supported:

* ``openai:<model>``  - the OpenAI chat completions HTTP API (temperature 0).
* ``cursor:<model>``  - the local ``cursor-agent`` CLI in read-only ``ask`` mode, run from an empty
  directory. Temperature cannot be set, and the CLI adds its own agent system prompt, so these
  are "model behind the Cursor agent harness", not the bare model.

Every call goes through :class:`CachedBackend`, which stores the full record (text, token usage,
latency) keyed by backend name and request, so re-runs make no model calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strata.llm import ModelRequest, ModelResponse

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
CURSOR_WORKDIR = Path("/tmp/strata-bench-empty-workspace")
RATE_LIMIT_RETRIES = 10  # backoff 30s doubling, capped at 300s: about 35 minutes in total


class BackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class Record:
    text: str
    in_tokens: int
    out_tokens: int
    latency_s: float
    cached: bool = False

    def as_json(self) -> str:
        return json.dumps(
            {
                "text": self.text,
                "in": self.in_tokens,
                "out": self.out_tokens,
                "latency_s": self.latency_s,
            }
        )

    @classmethod
    def from_json(cls, raw: str, cached: bool) -> Record:
        d = json.loads(raw)
        return cls(d["text"], d["in"], d["out"], d["latency_s"], cached)


class OpenAIBackend:
    def __init__(self, model: str) -> None:
        self.model = model
        self.name = f"openai:{model}"
        self._key = os.environ["OPENAI_API_KEY"]

    def call(self, request: ModelRequest) -> Record:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        payload = json.dumps(body).encode()
        last: Exception | None = None
        for attempt in range(8):
            req = urllib.request.Request(
                OPENAI_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
            )
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.load(resp)
                elapsed = time.perf_counter() - started
                usage = data.get("usage", {})
                return Record(
                    data["choices"][0]["message"]["content"] or "",
                    int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)),
                    elapsed,
                )
            except urllib.error.HTTPError as exc:
                last = exc
                detail = exc.read()[:300]
                retryable = exc.code in (408, 409, 429, 500, 502, 503, 504)
                if not retryable or b"insufficient_quota" in detail:
                    raise BackendError(f"{self.name}: HTTP {exc.code}: {detail!r}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(min(60, 2**attempt))
        raise BackendError(f"{self.name}: gave up after retries: {last}")


class CursorBackend:
    def __init__(self, model: str) -> None:
        self.model = model
        self.name = f"cursor:{model}"
        CURSOR_WORKDIR.mkdir(parents=True, exist_ok=True)

    def call(self, request: ModelRequest) -> Record:
        prompt = f"{request.system}\n\n{request.prompt}" if request.system else request.prompt
        cmd = [
            "cursor-agent",
            "-p",
            "--trust",
            "--model",
            self.model,
            "--mode",
            "ask",
            "--output-format",
            "json",
            "--workspace",
            str(CURSOR_WORKDIR),
            prompt,
        ]
        last = ""
        attempt = rate_limited = 0
        while attempt < 4 and rate_limited < RATE_LIMIT_RETRIES:
            started = time.perf_counter()
            try:
                proc = subprocess.run(
                    cmd, cwd=CURSOR_WORKDIR, capture_output=True, text=True, timeout=300
                )
            except subprocess.TimeoutExpired:
                last = "timeout"
                attempt += 1
                continue
            elapsed = time.perf_counter() - started
            if "rate limit" in (proc.stdout + proc.stderr).lower():
                last = "rate limited"
                time.sleep(min(300, 30 * 2**rate_limited))
                rate_limited += 1
                continue
            attempt += 1
            try:
                data = json.loads(proc.stdout.strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                last = (proc.stdout + proc.stderr)[:300]
                time.sleep(2**attempt)
                continue
            if data.get("is_error") or data.get("subtype") != "success":
                last = str(data)[:300]
                time.sleep(2**attempt)
                continue
            usage = data.get("usage", {})
            return Record(
                str(data.get("result", "")),
                int(usage.get("inputTokens", 0)),
                int(usage.get("outputTokens", 0)),
                elapsed,
            )
        raise BackendError(f"{self.name}: failed: {last}")


class ResponseStore:
    """SQLite response cache usable from many threads."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, record TEXT NOT NULL)"
        )
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT record FROM responses WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def put(self, key: str, record: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?)", (key, record))


class CachedBackend:
    """Cache wrapper; also satisfies ``strata.llm.Model`` so it can be used with Strata helpers."""

    def __init__(self, backend: OpenAIBackend | CursorBackend, store: ResponseStore) -> None:
        self._backend = backend
        self._store = store
        self.name = backend.name
        self._lock = threading.Lock()
        self.live_calls = 0
        self.cache_hits = 0

    def call(self, request: ModelRequest) -> Record:
        key = request.cache_key(self.name)
        raw = self._store.get(key)
        if raw is not None:
            with self._lock:
                self.cache_hits += 1
            return Record.from_json(raw, cached=True)
        record = self._backend.call(request)
        self._store.put(key, record.as_json())
        with self._lock:
            self.live_calls += 1
        return record

    def complete(self, request: ModelRequest) -> ModelResponse:
        record = self.call(request)
        return ModelResponse(record.text, self.name, record.cached)


def make_backend(spec: str, store: ResponseStore) -> CachedBackend:
    kind, _, model = spec.partition(":")
    if kind == "openai":
        return CachedBackend(OpenAIBackend(model), store)
    if kind == "cursor":
        return CachedBackend(CursorBackend(model), store)
    raise ValueError(f"unknown backend spec {spec!r}; use openai:<model> or cursor:<model>")
