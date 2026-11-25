"""Spread-logger rows (option_spreads)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .base import BaseRepo

COLS = ("ts", "minutes_from_open", "underlying", "symbol", "kind", "dte", "strike", "bid", "ask",
        "mid", "spread_pct", "iv", "delta", "volume", "open_interest", "underlying_price",
        "quote_age_s")


class OptionSpreadsRepo(BaseRepo):
    def add_option_spreads(self, rows: list[dict]) -> None:
        if not rows:
            return
        sql = f"INSERT INTO option_spreads ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})"
        self.conn.executemany(sql, [tuple(r.get(c) for c in COLS) for r in rows])

    def option_spreads(self, days: int = 20) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat(timespec="seconds")
        return self._rows("SELECT * FROM option_spreads WHERE ts >= ? ORDER BY ts", since)
