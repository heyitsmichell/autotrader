"""0008: option_outcomes table (closed auto option trades, for the options ML)."""

from __future__ import annotations


def upgrade(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS option_outcomes (
        proposal_id INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL, underlying TEXT, entry_premium REAL, exit_premium REAL, ret REAL,
        exit_reason TEXT, opened_at TEXT, closed_at TEXT, selection_json TEXT,
        ml_p REAL, explore INTEGER, recorded_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_option_outcomes_closed ON option_outcomes(closed_at)")
