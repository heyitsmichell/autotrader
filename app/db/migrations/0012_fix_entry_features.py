"""0012: repair trade_ledger.entry_features rows that were JSON-encoded twice.

Signal features could already be JSON text and were json.dumps'ed again on insert, so a single
json.loads returned a string (ML training then failed with "'str' object has no attribute 'get'").
Idempotent: rows that already decode to an object are left alone.
"""

from __future__ import annotations

import json


def upgrade(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_ledger)")}
    if "entry_features" not in cols:
        return
    rows = conn.execute("SELECT id, entry_features FROM trade_ledger WHERE entry_features IS NOT NULL").fetchall()
    for rid, text in rows:
        try:
            value = json.loads(text)
            if isinstance(value, str):
                value = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            fixed = json.dumps(value)
            if fixed != text:
                conn.execute("UPDATE trade_ledger SET entry_features=? WHERE id=?", (fixed, rid))
