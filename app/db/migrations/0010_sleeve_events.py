"""0010: sleeve_events (docs/SLEEVES_ARCHITECTURE.md section 8), namespaced like the other sleeve tables."""

from __future__ import annotations


def upgrade(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS sleeve_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT NOT NULL, ts TEXT NOT NULL, day TEXT NOT NULL,
        sleeve TEXT, kind TEXT NOT NULL, text TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_sleeve_events_day ON sleeve_events(ns, day, id)")
