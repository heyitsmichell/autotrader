"""0002: retention indexes plus the atm_iv_history and run_params tables."""

from __future__ import annotations

STATEMENTS = (
    # (symbol, ts) / (ts) indexes on the tables DB.prune() trims. news_items has no `ts`:
    # its time column is fetched_at. events, llm_usage and equity_snapshots have no symbol.
    "CREATE INDEX IF NOT EXISTS ix_news_items_symbol_fetched ON news_items(symbol, fetched_at)",
    "CREATE INDEX IF NOT EXISTS ix_news_items_fetched ON news_items(fetched_at)",
    "CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)",
    "CREATE INDEX IF NOT EXISTS ix_llm_usage_ts ON llm_usage(ts)",
    "CREATE INDEX IF NOT EXISTS ix_equity_snapshots_ts ON equity_snapshots(ts)",
    # Daily ATM implied vol per underlying: input for a true IV rank (H3).
    """CREATE TABLE IF NOT EXISTS atm_iv_history (
        underlying TEXT NOT NULL,
        day        TEXT NOT NULL,
        iv         REAL,
        PRIMARY KEY (underlying, day)
    )""",
    # Effective-parameter snapshot per run (M9 provenance).
    """CREATE TABLE IF NOT EXISTS run_params (
        run_id     INTEGER PRIMARY KEY,
        params     TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
)


def upgrade(conn) -> None:
    for sql in STATEMENTS:
        conn.execute(sql)
