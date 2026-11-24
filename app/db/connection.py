"""SQLite connection: PRAGMAs plus a lock that serializes every statement.

The app shares ONE connection between the event loop and worker threads
(``check_same_thread=False``). SQLite connections are not safe for concurrent use, so
every execution goes through ``LockedConnection``, which holds an ``RLock`` for the whole
statement *and* fetches its rows before releasing it (reads included).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

BUSY_TIMEOUT_MS = 5000


class _Result:
    """A cursor stand-in whose rows were fetched while the lock was still held."""

    __slots__ = ("_rows", "_i", "lastrowid", "rowcount", "description")

    def __init__(self, rows: list, lastrowid: int | None, rowcount: int, description: Any) -> None:
        self._rows = rows
        self._i = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self.description = description

    def fetchone(self):
        if self._i < len(self._rows):
            row = self._rows[self._i]
            self._i += 1
            return row
        return None

    def fetchall(self) -> list:
        rows = self._rows[self._i:]
        self._i = len(self._rows)
        return rows

    def fetchmany(self, size: int = 1) -> list:
        rows = self._rows[self._i:self._i + size]
        self._i += len(rows)
        return rows

    def __iter__(self) -> Iterator:
        return iter(self.fetchall())


class LockedConnection:
    """Thin proxy over ``sqlite3.Connection`` with the same ``execute`` surface."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self.raw = raw
        self.lock = threading.RLock()
        self.closed = False

    def execute(self, sql: str, params: Any = ()) -> _Result:
        with self.lock:
            cur = self.raw.execute(sql, params)
            rows = cur.fetchall() if cur.description is not None else []
            return _Result(rows, cur.lastrowid, cur.rowcount, cur.description)

    def executemany(self, sql: str, seq: Any) -> _Result:
        with self.lock:
            cur = self.raw.executemany(sql, seq)
            return _Result([], cur.lastrowid, cur.rowcount, None)

    def executescript(self, script: str) -> None:
        with self.lock:
            self.raw.executescript(script)

    def backup(self, target: sqlite3.Connection, **kw: Any) -> None:
        with self.lock:
            self.raw.backup(target, **kw)

    @property
    def row_factory(self):
        return self.raw.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self.raw.row_factory = value

    def close(self) -> None:
        """Checkpoint the WAL, then close. Idempotent."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error:
                pass
            self.raw.close()

    def __getattr__(self, name: str) -> Any:   # anything else: straight to sqlite3
        return getattr(self.raw, name)


def connect(path: Path | str) -> LockedConnection:
    """Open the database with the production PRAGMAs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path, check_same_thread=False, isolation_level=None,
                          timeout=BUSY_TIMEOUT_MS / 1000)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    raw.execute("PRAGMA synchronous=NORMAL")
    raw.execute("PRAGMA foreign_keys=ON")
    return LockedConnection(raw)
