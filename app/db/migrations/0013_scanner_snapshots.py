"""0013: point-in-time Webull scanner snapshots (read-only log, see app/scanner_log.py)."""

from __future__ import annotations


def upgrade(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS scanner_snapshots (
        day TEXT NOT NULL, scanner TEXT NOT NULL, rank INTEGER NOT NULL, symbol TEXT NOT NULL,
        fields_json TEXT, PRIMARY KEY (day, scanner, rank)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_scanner_symbol ON scanner_snapshots(symbol, day)")
