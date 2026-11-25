"""ShadowManager: virtual-money shadow strategies (docs/SHADOWS.md).

Hard rule: this module never calls a broker order method. It reads prices (``quote``, ``last_price``)
and keeps its own cash, holdings and trades in the ``shadow_*`` tables, which the live sleeves and the
swing engine never see. ``tick()`` never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime

from ..broker import BrokerError
from ..market import market_is_open, now_et, now_utc
from ..sleeves import data
from ..sleeves.manager import next_rebalance_day
from . import registry, strategies

log = logging.getLogger("app.shadows")
TICK_SECONDS = 60.0
PRICE_REFRESH_SECONDS = 300.0
COST_BPS = 10.0
TOP_N = 10


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _meta(row: dict | None) -> dict:
    try:
        return json.loads((row or {}).get("meta_json") or "{}")
    except ValueError:
        return {}


class ShadowManager:
    def __init__(self, eng) -> None:
        self.eng = eng
        self.s = eng.s
        self.db = eng.db
        self.broker = eng.broker
        self.running = False
        self._last_tick = 0.0
        self.last_error: str | None = None
        self._px: dict[str, dict] = {}            # shadow -> {"at": monotonic, "prices": {sym: px}}
        self._yahoo: dict[str, dict[str, float]] = {}   # shadow -> last Yahoo close per symbol
        self._retry_after: dict[str, float] = {}
        self._price_warned: dict[str, float] = {}

    # ------------------------------------------------------------------ scheduling
    def due(self) -> bool:
        return (bool(getattr(self.s, "shadows_enabled", False)) and not self.running
                and time.monotonic() - self._last_tick >= TICK_SECONDS)

    async def tick(self) -> None:
        if self.running:
            return
        self.running = True
        self._last_tick = time.monotonic()
        try:
            await self._tick()
            self.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - shadows must never disturb the engine
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("shadow tick failed")
        finally:
            self.running = False

    async def _tick(self) -> None:
        if not getattr(self.s, "shadows_enabled", False):
            return
        self._ensure_states()
        window = self._trading_window()
        for d in registry.SHADOWS:
            try:
                if window and self._due_month(d.id):
                    await self._rebalance(d)
                await self._value(d.id)
            except Exception as e:  # noqa: BLE001 - one shadow failing must not stop the others
                self.last_error = f"{d.id}: {type(e).__name__}: {e}"
                log.warning("shadow %s failed: %s", d.id, e, exc_info=True)

    def _trading_window(self) -> bool:
        now = now_et()
        if not market_is_open(now):
            return False
        hh, mm = (self.s.sleeve_rebalance_after_et or "10:00").split(":")
        return now.time() >= now.time().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)

    def _due_month(self, sid: str) -> bool:
        last = (self.db.shadow_state(sid) or {}).get("last_rebalance")
        if not last:
            return True
        d = datetime.fromisoformat(last).date()
        today = now_et().date()
        return (d.year, d.month) != (today.year, today.month)

    # ------------------------------------------------------------------ state
    def _ensure_states(self) -> None:
        today = now_et().date().isoformat()
        cap = float(self.s.shadow_start_capital)
        for d in registry.SHADOWS:
            if not self.db.shadow_state(d.id):
                self.db.upsert_shadow_state(d.id, cash=cap, start_capital=cap, start_date=today, peak_equity=cap)
                self.db.log("info", f"Shadow {d.name} started with ${cap:,.0f} virtual cash (no orders)")

    def _cash(self, sid: str) -> float:
        return float((self.db.shadow_state(sid) or {}).get("cash") or 0.0)

    # ------------------------------------------------------------------ prices (read-only broker calls)
    async def _price(self, sid: str, sym: str) -> float | None:
        p = None
        try:
            q = await self.broker.quote(sym)
            bid, ask = _f(getattr(q, "bid", None)), _f(getattr(q, "ask", None))
            if bid and ask:
                p = (bid + ask) / 2
            else:
                p = bid or ask
        except Exception:  # noqa: BLE001 - any read failure falls through to the next source
            p = None
        if not p:
            try:
                p = _f(await self.broker.last_price(sym))
            except Exception:  # noqa: BLE001
                p = None
        if not p:
            p = self._yahoo.get(sid, {}).get(sym)
        return float(p) if p else None

    async def _value(self, sid: str) -> None:
        """Refresh prices at most every 5 minutes, then record today's equity."""
        cache = self._px.setdefault(sid, {"at": -1e9, "prices": {}})
        holds = self.db.shadow_holdings(sid)
        if holds and time.monotonic() - cache["at"] >= PRICE_REFRESH_SECONDS:
            for h in holds:
                p = await self._price(sid, h["symbol"])
                if p:
                    cache["prices"][h["symbol"]] = p
            cache["at"] = time.monotonic()
        eq, _ = self._equity(sid)
        self.db.record_shadow_equity(now_et().date().isoformat(), sid, eq)
        st = self.db.shadow_state(sid) or {}
        peak = float(st.get("peak_equity") or eq)
        if eq > peak:
            self.db.upsert_shadow_state(sid, peak_equity=eq)

    def _equity(self, sid: str) -> tuple[float, float]:
        """(equity, invested) from cash plus holdings at the cached price (cost as the fallback)."""
        prices = self._px.get(sid, {}).get("prices", {})
        cash = self._cash(sid)
        invested = sum(float(h["qty"]) * float(prices.get(h["symbol"]) or h.get("avg_cost") or 0.0)
                       for h in self.db.shadow_holdings(sid))
        return round(cash + invested, 2), round(invested, 2)

    # ------------------------------------------------------------------ rebalance (virtual fills)
    async def _targets(self, d: registry.ShadowDef) -> dict[str, float] | None:
        meta = _meta(self.db.shadow_state(d.id))
        month = now_et().strftime("%Y-%m")
        if meta.get("targets_month") == month and meta.get("targets") is not None:
            return meta["targets"]
        if time.monotonic() < self._retry_after.get(d.id, 0):
            return None
        ohlc = await asyncio.to_thread(data.daily_ohlc, d.symbols())
        close = ohlc.get("close")
        if close is not None and not close.empty:
            self._yahoo[d.id] = {str(c): float(close[c].dropna().iloc[-1]) for c in close.columns
                                 if close[c].notna().any()}
        tg = strategies.compute(d.id, ohlc, TOP_N)
        if not tg:
            self._retry_after[d.id] = time.monotonic() + 1800
            self.db.log("warn", f"Shadow {d.id}: could not compute targets (price history missing); "
                                "retrying in 30 min")
            return None
        meta.update({"targets": tg, "targets_month": month, "targets_at": now_utc().isoformat(timespec="seconds")})
        self.db.upsert_shadow_state(d.id, meta=meta)
        return tg

    async def _rebalance(self, d: registry.ShadowDef) -> None:
        sid = d.id
        tg = await self._targets(d)
        if tg is None:
            return
        held = {h["symbol"]: h for h in self.db.shadow_holdings(sid)}
        prices: dict[str, float] = {}
        for sym in set(tg) | set(held):
            p = await self._price(sid, sym)
            if p:
                prices[sym] = p
        if any(s not in prices for s in tg):
            if time.monotonic() >= self._price_warned.get(sid, 0):     # at most one warning per hour
                self._price_warned[sid] = time.monotonic() + 3600
                self.db.log("warn", f"Shadow {sid}: no price for {sorted(s for s in tg if s not in prices)}; "
                                    "retrying every minute")
            return
        cost = COST_BPS / 1e4
        equity = self._cash(sid) + sum(float(h["qty"]) * prices.get(s, float(h.get("avg_cost") or 0))
                                       for s, h in held.items())
        want = {s: int(math.floor(equity * w / (prices[s] * (1 + cost)))) for s, w in tg.items()}
        # sells first (names that dropped out, and trims), then buys from the cash raised
        for sym, h in sorted(held.items()):
            q = float(h["qty"])
            tgt = want.get(sym, 0)
            if q > tgt and sym in prices:
                n = int(math.floor(q)) - tgt if tgt else int(math.floor(q))
                if n <= 0:
                    continue
                px = round(prices[sym] * (1 - cost), 4)
                self.db.set_shadow_holding(sid, sym, q - n, h.get("avg_cost"))
                self.db.upsert_shadow_state(sid, cash=round(self._cash(sid) + n * px, 2))
                self.db.add_shadow_trade(sid, sym, "SELL", n, px, "rebalance")
        held = {h["symbol"]: h for h in self.db.shadow_holdings(sid)}
        for sym in sorted(want, key=lambda s: -(want[s] - float((held.get(s) or {}).get("qty") or 0))):
            cur = float((held.get(sym) or {}).get("qty") or 0)
            px = round(prices[sym] * (1 + cost), 4)
            n = min(want[sym] - int(cur), int(math.floor(self._cash(sid) / px)))
            if n < 1:
                continue
            c0 = _f((held.get(sym) or {}).get("avg_cost")) or px
            self.db.set_shadow_holding(sid, sym, cur + n, round((cur * c0 + n * px) / (cur + n), 4))
            self.db.upsert_shadow_state(sid, cash=round(self._cash(sid) - n * px, 2))
            self.db.add_shadow_trade(sid, sym, "BUY", n, px, "rebalance")
        self._px.setdefault(sid, {"at": -1e9, "prices": {}})["prices"].update(prices)
        self._px[sid]["at"] = time.monotonic()
        self.db.upsert_shadow_state(sid, last_rebalance=now_et().isoformat(timespec="seconds"))
        top = ", ".join(f"{k} {v:.0%}" for k, v in sorted(tg.items(), key=lambda kv: -kv[1]))
        self.db.log("info", f"Shadow {d.name}: rebalanced (virtual) to {top}")

    # ------------------------------------------------------------------ controls
    def reset(self, sid: str) -> None:
        registry.get(sid)
        self.db.reset_shadow(sid)
        self._px.pop(sid, None)
        self._yahoo.pop(sid, None)
        self._retry_after.pop(sid, None)
        cap = float(self.s.shadow_start_capital)
        self.db.upsert_shadow_state(sid, cash=cap, start_capital=cap,
                                    start_date=now_et().date().isoformat(), peak_equity=cap)
        self._last_tick = 0.0
        self.db.log("info", f"Shadow {registry.get(sid).name} reset to ${cap:,.0f} virtual cash")

    # ------------------------------------------------------------------ API views
    def _view(self, d: registry.ShadowDef, series: list[dict]) -> dict:
        st = self.db.shadow_state(d.id) or {}
        started = bool(st)
        eq, _inv = self._equity(d.id) if started else (None, None)
        start = _f(st.get("start_capital")) or float(self.s.shadow_start_capital)
        peak = _f(st.get("peak_equity")) or eq
        if eq is not None and peak is not None:
            peak = max(peak, eq)
        pts = [r[d.id] for r in series if r.get(d.id) is not None]
        rets = [b / a - 1 for a, b in zip(pts, pts[1:]) if a]
        sharpe = None
        if len(rets) >= 20:
            m = sum(rets) / len(rets)
            sd = (sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
            sharpe = round(m / sd * math.sqrt(252), 2) if sd > 0 else None
        last_rb = st.get("last_rebalance")
        nxt = (next_rebalance_day(datetime.fromisoformat(last_rb).date()).isoformat()
               if last_rb else now_et().date().isoformat())
        return {
            "id": d.id, "name": d.name, "description": d.description, "rule": d.rule,
            "start_date": st.get("start_date"), "start_capital": start,
            "equity": eq, "cash": round(self._cash(d.id), 2) if started else None,
            "total_return_pct": round((eq / start - 1) * 100, 2) if eq is not None and start else None,
            "drawdown_pct": round((eq / peak - 1) * 100, 2) if eq is not None and peak else None,
            "positions_n": len(self.db.shadow_holdings(d.id)) if started else 0,
            "last_rebalance": last_rb, "next_rebalance": nxt,
            "live": {"days": len(rets), "sharpe": sharpe},
            "expected": d.expected,
        }

    def summary(self) -> dict:
        series = self.db.shadow_equity_series(3650)
        return {"enabled": bool(getattr(self.s, "shadows_enabled", False)),
                "shadows": [self._view(d, series) for d in registry.SHADOWS]}

    def detail(self, sid: str) -> dict:
        d = registry.get(sid)
        base = self._view(d, self.db.shadow_equity_series(3650))
        eq = base.get("equity") or 0
        prices = self._px.get(sid, {}).get("prices", {})
        holdings = []
        for h in self.db.shadow_holdings(sid):
            q, cost = float(h["qty"]), _f(h.get("avg_cost"))
            last = prices.get(h["symbol"]) or cost
            mv = q * (last or 0)
            holdings.append({"symbol": h["symbol"], "qty": q, "avg_cost": cost, "last": last,
                             "market_value": round(mv, 2),
                             "weight_pct": round(mv / eq * 100, 2) if eq else None})
        return {**base, "holdings": holdings, "trades": self.db.shadow_trades(sid, 50)}

    def equity_series(self, days: int = 365) -> dict:
        return {"series": self.db.shadow_equity_series(days)}
