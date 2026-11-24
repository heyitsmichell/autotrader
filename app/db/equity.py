"""Equity snapshots and the daily benchmark"""

from __future__ import annotations

from typing import Any

from .schema import _now
from .base import BaseRepo


class EquityRepo(BaseRepo):
    """Mixin for the DB facade: equity snapshots and the daily benchmark."""

    # -- equity ------------------------------------------------------------------
    def add_equity(self, b: dict) -> None:
        self._insert("equity_snapshots", {
            "ts": _now(), "net_liq": b.get("net_liq"), "cash": b.get("cash"),
            "market_value": b.get("market_value"), "unrealized": b.get("unrealized"),
            "day_pnl": b.get("day_pnl"), "buying_power": b.get("buying_power"),
            "broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id})

    def equity(self, since: str) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        # net_liq > 0 hides all-zero rows an earlier version stored when the broker answered
        # with an empty balance; they are not real account values.
        return self._rows(f"SELECT ts, net_liq, day_pnl FROM equity_snapshots WHERE ts >= ? AND net_liq > 0 AND {ns_where} ORDER BY id", since, *ns_args)

    def equity_full(self, since: str) -> list[dict]:
        """Equity snapshots with cash/market value, for the daily benchmark job."""
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT ts, net_liq, cash, market_value, day_pnl FROM equity_snapshots "
            f"WHERE ts >= ? AND net_liq > 0 AND {ns_where} ORDER BY id", since, *ns_args)

    # -- daily benchmark -----------------------------------------------------------
    def upsert_benchmark_daily(self, day: str, **fields: Any) -> None:
        """Insert or replace one completed day for this broker/account namespace."""
        allowed = {"net_liq", "cash", "market_value", "spy_close", "day_pnl", "flow",
                   "core_value", "satellite_value"}
        values = {k: v for k, v in fields.items() if k in allowed}
        values.setdefault("flow", 0.0)
        values.update(key=self._ns_key(day), day=day, updated_at=_now(),
                      broker_mode=self.broker_mode,
                      webull_environment=self.webull_environment,
                      account_id=self.account_id)
        cols = list(values)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "key")
        self.conn.execute(
            f"INSERT INTO benchmark_daily ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(key) DO UPDATE SET {updates}",
            tuple(values.values()))

    def benchmark_daily(self, since: str | None = None, limit: int = 5000) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        where = [ns_where]
        args: list = [*ns_args]
        if since:
            where.append("day >= ?")
            args.append(since)
        return self._rows(
            f"SELECT * FROM benchmark_daily WHERE {' AND '.join(where)} "
            f"ORDER BY day ASC LIMIT ?", *args, limit)

    def benchmark_before(self, day: str) -> dict | None:
        ns_where, ns_args = self._ns_where()
        return self._one(
            f"SELECT * FROM benchmark_daily WHERE day < ? AND {ns_where} "
            f"ORDER BY day DESC LIMIT 1", day, *ns_args)

    def benchmark_trades(self, start: str, end: str, limit: int = 10000) -> list[dict]:
        """Trade-ledger fills spanning a date window, for turnover."""
        ns_where, ns_args = self._ns_where()
        start_ts, end_ts = f"{start}T00:00:00", f"{end}T23:59:59"
        return self._rows(
            f"SELECT * FROM trade_ledger WHERE {ns_where} AND ("
            f"(entry_fill_time BETWEEN ? AND ?) OR (exit_fill_time BETWEEN ? AND ?)) "
            f"ORDER BY id LIMIT ?",
            *ns_args, start_ts, end_ts, start_ts, end_ts, limit)

    def prune_equity(self, before: str) -> None:
        ns_where, ns_args = self._ns_where()
        self.conn.execute(f"DELETE FROM equity_snapshots WHERE ts < ? AND {ns_where}", (before, *ns_args))
