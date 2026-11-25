"""0009: multi-strategy sleeves (docs/SLEEVES_ARCHITECTURE.md section 3).

Every table carries ``ns`` (the kv-style namespace ``mode:env:account``) so paper and live rows
never mix.
"""

from __future__ import annotations


def upgrade(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS sleeve_state (
        sleeve TEXT NOT NULL, ns TEXT NOT NULL, cash REAL, start_capital REAL, start_date TEXT,
        peak_equity REAL, paused INTEGER NOT NULL DEFAULT 0, pause_reason TEXT,
        last_rebalance TEXT, meta_json TEXT, updated_at TEXT,
        PRIMARY KEY (sleeve, ns)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sleeve_holdings (
        sleeve TEXT NOT NULL, ns TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
        qty REAL NOT NULL, avg_cost REAL, opened_at TEXT, updated_at TEXT, meta_json TEXT,
        PRIMARY KEY (sleeve, symbol, ns)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sleeve_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ns TEXT NOT NULL, ts TEXT NOT NULL,
        sleeve TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL, side TEXT NOT NULL,
        qty REAL NOT NULL, limit_price REAL, client_order_id TEXT, status TEXT NOT NULL,
        filled_qty REAL NOT NULL DEFAULT 0, avg_fill_price REAL, position_intent TEXT,
        reason TEXT, meta_json TEXT, updated_at TEXT
    )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_sleeve_orders_coid ON sleeve_orders(client_order_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_sleeve_orders_sleeve ON sleeve_orders(ns, sleeve, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS sleeve_equity (
        day TEXT NOT NULL, sleeve TEXT NOT NULL, ns TEXT NOT NULL, equity REAL, cash REAL,
        invested REAL, PRIMARY KEY (day, sleeve, ns)
    )""")
