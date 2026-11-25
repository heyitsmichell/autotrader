"""0011: shadow strategies (docs/SHADOWS.md): virtual-only tables, namespaced like the sleeve tables."""

from __future__ import annotations


def upgrade(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS shadow_state (
        shadow TEXT NOT NULL, ns TEXT NOT NULL, cash REAL, start_capital REAL, start_date TEXT,
        peak_equity REAL, last_rebalance TEXT, meta_json TEXT, updated_at TEXT,
        PRIMARY KEY (shadow, ns)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS shadow_holdings (
        shadow TEXT NOT NULL, ns TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_cost REAL,
        updated_at TEXT, PRIMARY KEY (shadow, symbol, ns)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS shadow_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT NOT NULL, ts TEXT NOT NULL, shadow TEXT NOT NULL,
        symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL, reason TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_shadow_trades ON shadow_trades(ns, shadow, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS shadow_equity (
        day TEXT NOT NULL, shadow TEXT NOT NULL, ns TEXT NOT NULL, equity REAL,
        PRIMARY KEY (day, shadow, ns)
    )""")
