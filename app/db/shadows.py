"""Shadow strategy rows (docs/SHADOWS.md): virtual money only, separate from the sleeve tables."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from .base import BaseRepo
from .schema import _now

_STATE_FIELDS = {"cash", "start_capital", "start_date", "peak_equity", "last_rebalance", "meta_json"}


class ShadowsRepo(BaseRepo):
    @property
    def _shadow_ns(self) -> str:
        return f"{self.broker_mode}:{self.webull_environment or 'none'}:{self.account_id or 'none'}"

    def shadow_state(self, shadow: str) -> dict | None:
        return self._one("SELECT * FROM shadow_state WHERE shadow=? AND ns=?", shadow, self._shadow_ns)

    def upsert_shadow_state(self, shadow: str, **fields: Any) -> None:
        bad = set(fields) - _STATE_FIELDS - {"meta"}
        if bad:
            raise ValueError(f"unknown shadow_state fields: {sorted(bad)}")
        if "meta" in fields:
            m = fields.pop("meta")
            fields["meta_json"] = m if m is None or isinstance(m, str) else json.dumps(m, default=str)
        fields["updated_at"] = _now()
        cols = ["shadow", "ns", *fields]
        vals = [shadow, self._shadow_ns, *fields.values()]
        upd = ", ".join(f"{c}=excluded.{c}" for c in fields)
        self.conn.execute(
            f"INSERT INTO shadow_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(shadow, ns) DO UPDATE SET {upd}", vals)

    def shadow_holdings(self, shadow: str) -> list[dict]:
        return self._rows("SELECT * FROM shadow_holdings WHERE ns=? AND shadow=? ORDER BY symbol",
                          self._shadow_ns, shadow)

    def set_shadow_holding(self, shadow: str, symbol: str, qty: float, avg_cost: float | None) -> None:
        """Set (not add) a holding; qty 0 deletes the row."""
        symbol = symbol.upper()
        if abs(float(qty)) < 1e-9:
            self.conn.execute("DELETE FROM shadow_holdings WHERE shadow=? AND symbol=? AND ns=?",
                              (shadow, symbol, self._shadow_ns))
            return
        self.conn.execute(
            "INSERT INTO shadow_holdings (shadow, ns, symbol, qty, avg_cost, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(shadow, symbol, ns) DO UPDATE SET qty=excluded.qty, avg_cost=excluded.avg_cost, "
            "updated_at=excluded.updated_at",
            (shadow, self._shadow_ns, symbol, float(qty), avg_cost, _now()))

    def add_shadow_trade(self, shadow: str, symbol: str, side: str, qty: float, price: float,
                         reason: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO shadow_trades (ns, ts, shadow, symbol, side, qty, price, reason) VALUES (?,?,?,?,?,?,?,?)",
            (self._shadow_ns, _now(), shadow, symbol.upper(), side.upper(), float(qty), float(price), reason))
        return int(cur.lastrowid)

    def shadow_trades(self, shadow: str, limit: int = 50) -> list[dict]:
        return self._rows("SELECT ts, symbol, side, qty, price, reason FROM shadow_trades WHERE ns=? AND shadow=? "
                          "ORDER BY id DESC LIMIT ?", self._shadow_ns, shadow, int(limit))

    def record_shadow_equity(self, day: str, shadow: str, equity: float | None) -> None:
        self.conn.execute(
            "INSERT INTO shadow_equity (day, shadow, ns, equity) VALUES (?,?,?,?) "
            "ON CONFLICT(day, shadow, ns) DO UPDATE SET equity=excluded.equity",
            (day, shadow, self._shadow_ns, equity))

    def shadow_equity_series(self, days: int = 365) -> list[dict]:
        """One row per day: {"day", <shadow id>: equity, ...}."""
        from ..market import now_et
        since = (now_et().date() - timedelta(days=int(days))).isoformat()
        out: dict[str, dict] = {}
        for r in self._rows("SELECT day, shadow, equity FROM shadow_equity WHERE ns=? AND day >= ? ORDER BY day",
                            self._shadow_ns, since):
            out.setdefault(r["day"], {"day": r["day"]})[r["shadow"]] = r["equity"]
        return list(out.values())

    def reset_shadow(self, shadow: str) -> None:
        for t in ("shadow_state", "shadow_holdings", "shadow_trades", "shadow_equity"):
            self.conn.execute(f"DELETE FROM {t} WHERE shadow=? AND ns=?", (shadow, self._shadow_ns))
