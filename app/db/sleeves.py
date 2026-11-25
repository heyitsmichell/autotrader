"""Multi-strategy sleeve rows (docs/SLEEVES_ARCHITECTURE.md sections 3-4)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Iterable

from .base import BaseRepo
from .schema import _now

OPEN_ORDER_STATUSES = ("submitting", "working", "partial")
_STATE_FIELDS = {"cash", "start_capital", "start_date", "peak_equity", "paused", "pause_reason",
                 "last_rebalance", "meta_json"}
_ORDER_FIELDS = {"status", "filled_qty", "avg_fill_price", "reason", "meta_json", "limit_price",
                 "client_order_id"}


def _meta(v: Any) -> str | None:
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v, default=str)


class SleevesRepo(BaseRepo):
    @property
    def _sleeve_ns(self) -> str:
        return f"{self.broker_mode}:{self.webull_environment or 'none'}:{self.account_id or 'none'}"

    # -- state ------------------------------------------------------------------------
    def sleeve_state(self, sleeve: str) -> dict | None:
        return self._one("SELECT * FROM sleeve_state WHERE sleeve=? AND ns=?", sleeve, self._sleeve_ns)

    def sleeve_states(self) -> dict[str, dict]:
        return {r["sleeve"]: r for r in self._rows("SELECT * FROM sleeve_state WHERE ns=?", self._sleeve_ns)}

    def upsert_sleeve_state(self, sleeve: str, **fields: Any) -> None:
        bad = set(fields) - _STATE_FIELDS - {"meta"}
        if bad:
            raise ValueError(f"unknown sleeve_state fields: {sorted(bad)}")
        if "meta" in fields:
            fields["meta_json"] = _meta(fields.pop("meta"))
        if "paused" in fields:
            fields["paused"] = 1 if fields["paused"] else 0
        fields["updated_at"] = _now()
        cols = ["sleeve", "ns", *fields]
        vals = [sleeve, self._sleeve_ns, *fields.values()]
        upd = ", ".join(f"{c}=excluded.{c}" for c in fields)
        self.conn.execute(
            f"INSERT INTO sleeve_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(sleeve, ns) DO UPDATE SET {upd}", vals)

    # -- holdings ---------------------------------------------------------------------
    def sleeve_holdings(self, sleeve: str | None = None) -> list[dict]:
        if sleeve:
            return self._rows("SELECT * FROM sleeve_holdings WHERE ns=? AND sleeve=? ORDER BY symbol",
                              self._sleeve_ns, sleeve)
        return self._rows("SELECT * FROM sleeve_holdings WHERE ns=? ORDER BY sleeve, symbol", self._sleeve_ns)

    def set_sleeve_holding(self, sleeve: str, symbol: str, kind: str, qty: float,
                           avg_cost: float | None, meta: Any = None) -> None:
        """Set (not add) a holding; qty 0 deletes the row."""
        symbol = symbol.upper()
        if abs(float(qty)) < 1e-9:
            self.conn.execute("DELETE FROM sleeve_holdings WHERE sleeve=? AND symbol=? AND ns=?",
                              (sleeve, symbol, self._sleeve_ns))
            return
        now = _now()
        self.conn.execute(
            "INSERT INTO sleeve_holdings (sleeve, ns, symbol, kind, qty, avg_cost, opened_at, updated_at, "
            "meta_json) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(sleeve, symbol, ns) DO UPDATE SET "
            "kind=excluded.kind, qty=excluded.qty, avg_cost=excluded.avg_cost, updated_at=excluded.updated_at, "
            "meta_json=COALESCE(excluded.meta_json, sleeve_holdings.meta_json)",
            (sleeve, self._sleeve_ns, symbol, kind, float(qty), avg_cost, now, now, _meta(meta)))

    def sleeve_reserved(self) -> dict[str, float]:
        """Shares held by the non-swing sleeves, per symbol. Broker.positions() subtracts these."""
        rows = self._rows("SELECT symbol, SUM(qty) AS q FROM sleeve_holdings WHERE ns=? AND kind='stock' "
                          "AND sleeve != 'swing' GROUP BY symbol", self._sleeve_ns)
        return {r["symbol"]: float(r["q"]) for r in rows if r["q"]}

    def sleeve_option_symbols(self) -> set[str]:
        """OCC symbols a sleeve holds or has a working order in (hidden from the option exit manager)."""
        held = self._rows("SELECT symbol FROM sleeve_holdings WHERE ns=? AND kind='option'", self._sleeve_ns)
        working = self._rows(
            f"SELECT symbol FROM sleeve_orders WHERE ns=? AND kind='option' AND status IN "
            f"({','.join('?' * len(OPEN_ORDER_STATUSES))})", self._sleeve_ns, *OPEN_ORDER_STATUSES)
        return {r["symbol"].upper() for r in held + working}

    # -- orders -----------------------------------------------------------------------
    def add_sleeve_order(self, sleeve: str, symbol: str, kind: str, side: str, qty: float,
                         limit_price: float | None, coid: str, position_intent: str | None = None,
                         reason: str | None = None, meta: Any = None, status: str = "submitting") -> int:
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO sleeve_orders (ns, ts, sleeve, symbol, kind, side, qty, limit_price, client_order_id, "
            "status, position_intent, reason, meta_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self._sleeve_ns, now, sleeve, symbol.upper(), kind, side.upper(), float(qty), limit_price, coid,
             status, position_intent, reason, _meta(meta), now))
        return int(cur.lastrowid)

    def update_sleeve_order(self, order_id: int, **fields: Any) -> None:
        bad = set(fields) - _ORDER_FIELDS - {"meta"}
        if bad:
            raise ValueError(f"unknown sleeve_orders fields: {sorted(bad)}")
        if "meta" in fields:
            fields["meta_json"] = _meta(fields.pop("meta"))
        fields["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE sleeve_orders SET {sets} WHERE id=? AND ns=?",
                          (*fields.values(), order_id, self._sleeve_ns))

    def sleeve_orders(self, sleeve: str | None = None, limit: int = 50,
                      statuses: Iterable[str] | None = None) -> list[dict]:
        where, args = ["ns=?"], [self._sleeve_ns]
        if sleeve:
            where.append("sleeve=?")
            args.append(sleeve)
        st = list(statuses or [])
        if st:
            where.append(f"status IN ({','.join('?' * len(st))})")
            args.extend(st)
        return self._rows(f"SELECT * FROM sleeve_orders WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                          *args, int(limit))

    def sleeve_coids(self) -> set[str]:
        rows = self._rows("SELECT client_order_id FROM sleeve_orders WHERE client_order_id IS NOT NULL AND ns=?",
                          self._sleeve_ns)
        return {r["client_order_id"] for r in rows}

    # -- equity -----------------------------------------------------------------------
    def record_sleeve_equity(self, day: str, sleeve: str, equity: float | None,
                             cash: float | None = None, invested: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO sleeve_equity (day, sleeve, ns, equity, cash, invested) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(day, sleeve, ns) DO UPDATE SET equity=excluded.equity, cash=excluded.cash, "
            "invested=excluded.invested", (day, sleeve, self._sleeve_ns, equity, cash, invested))

    def sleeve_equity_series(self, days: int = 365) -> list[dict]:
        """One row per day: {"day", <sleeve>: equity, ...} including 'total' and 'spy'."""
        from ..market import now_et
        since = (now_et().date() - timedelta(days=int(days))).isoformat()
        out: dict[str, dict] = {}
        for r in self._rows("SELECT day, sleeve, equity FROM sleeve_equity WHERE ns=? AND day >= ? ORDER BY day",
                            self._sleeve_ns, since):
            out.setdefault(r["day"], {"day": r["day"]})[r["sleeve"]] = r["equity"]
        return list(out.values())

    # -- events (migration 0010) --------------------------------------------------------
    def add_sleeve_event(self, sleeve: str | None, kind: str, text: str, ts: str | None = None) -> int:
        from ..market import now_et
        day = ts[:10] if ts else now_et().date().isoformat()     # event day = the ET trading date
        ts = ts or _now()
        cur = self.conn.execute("INSERT INTO sleeve_events (ns, ts, day, sleeve, kind, text) VALUES (?,?,?,?,?,?)",
                                (self._sleeve_ns, ts, day, sleeve, kind, text))
        return int(cur.lastrowid)

    def sleeve_events(self, days: int = 365, sleeve: str | None = None) -> list[dict]:
        from ..market import now_et
        since = (now_et().date() - timedelta(days=int(days))).isoformat()
        where, args = ["ns=?", "day >= ?"], [self._sleeve_ns, since]
        if sleeve:
            where.append("sleeve=?")
            args.append(sleeve)
        return self._rows(f"SELECT id, ts, day, sleeve, kind, text FROM sleeve_events WHERE {' AND '.join(where)} "
                          "ORDER BY id", *args)
