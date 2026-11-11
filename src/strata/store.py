"""SQLite connection management and write transactions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import index
from .errors import FTSUnavailableError, SchemaError, StrataError
from .schema import MIGRATE_2_TO_3, MIGRATE_3_TO_4, SCHEMA, SCHEMA_VERSION


def encode(value: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, no NaN/Infinity."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical-JSON serializable: {exc}") from exc


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class WriteTxn:
    """An open ``BEGIN IMMEDIATE`` transaction.

    A revision number is only allocated when first requested, so transactions
    that turn out to be no-ops (e.g. all-duplicate ingests) do not consume one.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._revision: int | None = None

    @property
    def revision(self) -> int:
        if self._revision is None:
            cur = self.db.execute("INSERT INTO revisions DEFAULT VALUES")
            self._revision = int(cur.lastrowid or 0)
        return self._revision

    @property
    def allocated(self) -> bool:
        return self._revision is not None


class Store:
    """Owns one SQLite connection. Not thread-safe; open one ``Store`` per thread/process."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self._initialise()
        except BaseException:
            self.db.close()
            raise

    def _initialise(self) -> None:
        db = self.db
        try:
            db.execute("CREATE VIRTUAL TABLE temp._fts_check USING fts5(x)")
            db.execute("DROP TABLE temp._fts_check")
        except sqlite3.OperationalError as exc:
            raise FTSUnavailableError("SQLite FTS5 is required") from exc
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
        if has_meta:
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is not None and row[0] == "2":
                self._migrate_2_to_3()
                row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is not None and row[0] == "3":
                self._run_script(MIGRATE_3_TO_4)
                row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] != str(SCHEMA_VERSION):
                found = None if row is None else row[0]
                raise SchemaError(
                    f"unsupported schema version {found!r}; this build supports {SCHEMA_VERSION}"
                )
        elif db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            raise SchemaError("database is not a Strata database")
        db.executescript(SCHEMA)
        db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        db.execute("PRAGMA foreign_keys=ON")

    def _run_script(self, script: str, refresh_index: bool = False) -> None:
        with self.write():
            for statement in script.split(";"):
                if statement.strip():
                    self.db.execute(statement)
            if refresh_index:
                seqs = self.db.execute("SELECT DISTINCT source_seq FROM fact_versions").fetchall()
                for (seq,) in seqs:
                    index.refresh_event(self.db, int(seq))

    def _migrate_2_to_3(self) -> None:
        self._run_script(MIGRATE_2_TO_3, refresh_index=True)

    @contextmanager
    def write(self) -> Iterator[WriteTxn]:
        if self.db.in_transaction:
            raise StrataError("nested write transactions are not supported")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield WriteTxn(self.db)
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()

    encode = staticmethod(encode)
