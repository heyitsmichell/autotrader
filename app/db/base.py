"""Shared helpers for the repository mixins."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .schema import ADDED_COLUMNS, add_column, sync_schema_columns, table_columns


class BaseRepo:
    """Namespace filtering and the small insert/update helpers every repository uses."""

    conn: Any
    broker_mode: str
    webull_environment: str | None
    account_id: str | None

    def transaction(self):
        """Re-entrant atomic block over the shared connection (see LockedConnection.transaction)."""
        return self.conn.transaction()

    def _heal_missing_column(self, table: str, err: sqlite3.OperationalError) -> bool:
        """Safety net for 'table X has no column named Y': add Y if the schema knows it."""
        m = re.search(r"has no column named (\w+)", str(err))
        if not m:
            return False
        col = m.group(1)
        if col in table_columns(self.conn, table):
            return True  # another process already added it; just retry
        sync_schema_columns(self.conn)
        for tbl, cols in ADDED_COLUMNS.items():
            if tbl == table and col in cols:
                add_column(self.conn, table, col, cols[col])
        return col in table_columns(self.conn, table)

    def _ns_where(self) -> tuple[str, list]:
        """Return WHERE clause and args for namespace filtering."""
        where = "broker_mode = ? AND webull_environment IS ? AND account_id IS ?"
        args = [self.broker_mode, self.webull_environment, self.account_id]
        return where, args

    def _ns_where_kv(self) -> tuple[str, list]:
        """Return WHERE clause and args for kv namespace filtering."""
        ns = f"{self.broker_mode}:{self.webull_environment or 'none'}:{self.account_id or 'none'}"
        where = "namespace = ?"
        args = [ns]
        return where, args

    def _rows(self, sql: str, *args: Any) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, *args: Any) -> dict | None:
        rows = self._rows(sql, *args)
        return rows[0] if rows else None

    def _insert(self, table: str, fields: dict) -> int:
        cols = list(fields)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        try:
            cur = self.conn.execute(sql, tuple(fields.values()))
        except sqlite3.OperationalError as e:
            # Self-heal once (e.g. a DB file from an older version) instead of failing the run.
            if not self._heal_missing_column(table, e):
                raise
            cur = self.conn.execute(sql, tuple(fields.values()))
        return int(cur.lastrowid)

    def _update(self, table: str, row_id: int, fields: dict) -> None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))
