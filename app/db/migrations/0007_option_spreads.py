"""0007: option_spreads table (spread logger: real bid/ask by time of day)."""

from __future__ import annotations


def upgrade(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS option_spreads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, minutes_from_open REAL, underlying TEXT NOT NULL, symbol TEXT NOT NULL,
        kind TEXT NOT NULL, dte INTEGER, strike REAL, bid REAL, ask REAL, mid REAL,
        spread_pct REAL, iv REAL, delta REAL, volume REAL, open_interest REAL,
        underlying_price REAL, quote_age_s REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_option_spreads_ts ON option_spreads(ts)")
