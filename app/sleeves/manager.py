"""SleeveManager: runs the momentum, put-write and VAA sleeves next to the swing engine.

Design (docs/SLEEVES_ARCHITECTURE.md):
- ``tick()`` runs about once a minute as a background task from the risk loop. It never raises.
- Each tick: settle the sleeve's working orders, value every sleeve, record equity, apply the
  drawdown pause, then advance at most ONE trading step per sleeve (a state machine, so a restart
  in the middle of a rebalance simply carries on from the holdings that are recorded).
- Share bookkeeping always errs toward the swing engine seeing MORE shares than it owns, never
  fewer: a BUY adds sleeve shares only once filled; a SELL removes them when it is sent (and adds
  any unfilled rest back). Seeing fewer shares could make swing code think its position is gone.
- Orders are marketable limits in the CORE session, split to respect MAX_ORDER_USD /
  MAX_ORDER_QTY / OPTIONS_MAX_CONTRACTS_PER_ORDER so the broker guard never refuses them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta
from statistics import NormalDist

from ..broker import BrokerError
from ..market import is_trading_day, market_is_open, now_et, now_utc
from ..portfolio import ACTIVE, parse_orders, parse_position_rows
from ..engine.util import parse_occ
from . import analytics, registry
from .strategies import momentum, putwrite, rebalance, vaa

log = logging.getLogger("app.sleeves")
_OCC_TOKEN = re.compile(r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b")
OPEN_STATUSES = ("submitting", "working", "partial")
TICK_SECONDS = 60.0
CANCEL_AFTER_SECONDS = 180.0       # an unfilled marketable limit is cancelled and re-tried next tick
GIVE_UP_SECONDS = 600.0            # never seen at the broker -> failed
OFFSET_BPS = 10.0                  # marketable limit offset beyond the bid/ask
DD_CONFIRM_TICKS = 3               # drawdown pause needs this many consecutive breaching ticks
MAX_PASSES_PER_MONTH = 12          # rebalance passes before a month is closed with a warning
VAA_SYMBOLS = list(dict.fromkeys(vaa.G12 + vaa.DEFENSIVE))


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


def next_rebalance_day(after: date) -> date:
    """First trading day of the month after ``after``."""
    d = (after.replace(day=1) + timedelta(days=32)).replace(day=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


class SleeveManager:
    def __init__(self, eng) -> None:
        self.eng = eng
        self.s = eng.s
        self.db = eng.db
        self.broker = eng.broker
        self.running = False
        self._last_tick = 0.0
        self.last_error: str | None = None
        self.last_values: dict[str, dict] = {}
        self._retry_after: dict[str, float] = {}
        self._dd_hits: dict[str, int] = {}
        self._last_eq: dict[str, float] = {}
        try:            # the swing daily-loss limit subtracts the other sleeves' day P&L (risk.py)
            self.broker.__dict__["sleeve_day_pnl"] = self.others_day_pnl
        except (AttributeError, TypeError):
            pass

    # ------------------------------------------------------------------ scheduling
    def due(self) -> bool:
        return (self.s.sleeves_enabled and not self.running
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
        except Exception as e:  # noqa: BLE001 - the sleeves must never disturb the swing engine
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("sleeve tick failed")
        finally:
            self.running = False

    def enabled_ids(self) -> list[str]:
        s = self.s
        on = {"momentum": s.sleeve_momentum_enabled, "putwrite": s.sleeve_putwrite_enabled,
              "vaa": s.sleeve_vaa_enabled}
        return [i for i in registry.NON_SWING if on.get(i)]

    async def _tick(self) -> None:
        if not self.s.sleeves_enabled:
            return
        self._ensure_states()
        self._check_kill_switch()
        self._check_settings_once()
        await self.sync_orders()
        snap = await self._snapshot()
        values = self._value(snap)
        self.last_values = values
        short = self.capital_shortfall(snap.get("net_liq"))
        if short:
            # the account cannot fund the sleeves: never trade, never record a negative swing sleeve
            day = now_et().date().isoformat()
            if self.db.get_kv("sleeves_capital_warned") != day:
                self.db.set_kv("sleeves_capital_warned", day)
                self.db.log("error", short, alert=True)
            return
<<<<<<< HEAD
        try:
            await self._price("SPY", snap)    # SPY is rarely held, so the snapshot lacks it
        except Exception:  # noqa: BLE001 - the benchmark line is optional, never block the tick
            pass
=======
>>>>>>> 2e32c5e54414a1984ef1cfefe57d83289c20ffb5
        self._record(values, snap)
        await self._drawdown_checks(values)
        self._drift_checks()
        if not self._trading_window():
            return
        for sid in self.enabled_ids():
            if self.db.sleeve_orders(sid, 20, OPEN_STATUSES):
                continue                     # one step at a time: wait for working orders
            try:
                if sid == "putwrite":
                    await self._putwrite_step(values, snap)
                else:
                    await self._rebalance_step(sid, values, snap)
            except BrokerError as e:
                self.db.log("warn", f"Sleeve {sid}: broker error ({e}); will retry next minute")

    # ------------------------------------------------------------------ events (never raise)
    def _event(self, sleeve: str | None, kind: str, text: str) -> None:
        try:
            self.db.add_sleeve_event(sleeve, kind, text)
        except Exception:  # noqa: BLE001 - an event is a nicety, never a reason to fail
            log.warning("could not record sleeve event %s", kind, exc_info=True)

    def _check_kill_switch(self) -> None:
        try:
            on = bool(self.eng.kill_switch)
            seen = self.db.get_kv("sleeves_kill_seen")
            self.db.set_kv("sleeves_kill_seen", on)
            if seen is None or bool(seen) == on:
                return
            self._event(None, "kill_on" if on else "kill_off",
                        "Kill switch turned ON" if on else "Kill switch turned off")
        except Exception:  # noqa: BLE001
            log.debug("kill switch event check failed", exc_info=True)

    @staticmethod
    def _fmt(v) -> str:
        if isinstance(v, float) and v == int(v):
            return str(int(v))
        return str(v)

    def _check_settings_once(self) -> None:
        if getattr(self, "_settings_checked", False):
            return
        self._settings_checked = True
        try:
            cur = {k: v for k, v in dataclasses.asdict(self.s).items()
                   if k.startswith(("sleeve_", "max_", "kill_", "risk_")) or k == "size_from_risk"}
            cur = {k: v if isinstance(v, (int, float, str, bool, type(None))) else str(v) for k, v in cur.items()}
            prev = self.db.get_kv("sleeves_settings_snapshot")
            self.db.set_kv("sleeves_settings_snapshot", cur)
            if not isinstance(prev, dict):
                return
            changes = [f"{k.upper()} {self._fmt(prev[k])} -> {self._fmt(cur[k])}"
                       for k in sorted(cur) if k in prev and prev[k] != cur[k]]
            if changes:
                self._event(None, "settings", "; ".join(changes))
        except Exception:  # noqa: BLE001
            log.debug("settings snapshot check failed", exc_info=True)

    def _drift_checks(self) -> None:
        # Once a day per sleeve: live return below the p5 band for >= 5 trading days -> alert + event.
        try:
            today = now_et().date().isoformat()
            if self.db.get_kv("sleeves_drift_day") == today:
                return
            self.db.set_kv("sleeves_drift_day", today)
            last = self.db.get_kv("sleeves_drift_last") or {}
            series = self.db.sleeve_equity_series(3650)
            ids = ["swing", *self.enabled_ids()]
            for sid in ids:
                pts = [(r["day"], r[sid]) for r in series if r.get(sid) is not None]
                band = analytics.band_for(sid, pts)
                status, below = analytics.drift_status(band)
                if status != "below" or below < 5:
                    continue
                prev = last.get(sid)
                if prev and (date.fromisoformat(today) - date.fromisoformat(prev)).days < 7:
                    continue
                last[sid] = today
                pt = band["points"][-1]
                msg = (f"Sleeve {registry.get(sid).name} is below its expected range for {below} trading days "
                       f"(live {pt['live']:+.1%} vs 5th percentile {pt['p5']:+.1%})")
                self.db.log("warn", msg, alert=True)
                self._event(sid, "drift", msg)
            self.db.set_kv("sleeves_drift_last", last)
        except Exception:  # noqa: BLE001
            log.debug("drift check failed", exc_info=True)

    def capital_shortfall(self, net_liq: float | None) -> str | None:
        """A message when net liq cannot cover every enabled sleeve plus a full swing sleeve."""
        if net_liq is None:
            return None
        need = float(self.s.sleeve_start_capital) * (len(self.enabled_ids()) + 1)
        if net_liq + 1e-6 < need * 0.95:
            return (f"Sleeves NOT running: net liquidation ${net_liq:,.0f} is below the ${need:,.0f} needed for "
                    f"{len(self.enabled_ids()) + 1} sleeves of ${self.s.sleeve_start_capital:,.0f}. "
                    "Lower SLEEVE_START_CAPITAL or turn sleeves off.")
        return None

    def _trading_window(self) -> bool:
        now = now_et()
        if not market_is_open(now):
            return False
        hh, mm = (self.s.sleeve_rebalance_after_et or "10:00").split(":")
        return now.time() >= now.time().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)

    # ------------------------------------------------------------------ state
    def _ensure_states(self) -> None:
        today = now_et().date().isoformat()
        cap = float(self.s.sleeve_start_capital)
        states = self.db.sleeve_states()
        for sid in registry.NON_SWING:
            if sid not in states:
                self.db.upsert_sleeve_state(sid, cash=cap, start_capital=cap, start_date=today,
                                            peak_equity=cap, paused=False)
                self.db.log("info", f"Sleeve {registry.get(sid).name} started with ${cap:,.0f} virtual cash")
        if "swing" not in states:
            self.db.upsert_sleeve_state("swing", start_date=today, paused=False)

    def _cash(self, sid: str) -> float:
        return float((self.db.sleeve_state(sid) or {}).get("cash") or 0.0)

    def _add_cash(self, sid: str, delta: float) -> None:
        self.db.upsert_sleeve_state(sid, cash=round(self._cash(sid) + delta, 2))

    # ------------------------------------------------------------------ valuation
    async def _snapshot(self) -> dict:
        text = await self.broker.positions_text(fresh=True)
        rows = parse_position_rows(text) if text and not text.startswith("No data") else []
        last = {r["symbol"]: r.get("last") for r in rows if r.get("last")}
        raw_qty = {r["symbol"]: float(r["qty"]) for r in rows}
        try:
            from ..engine.option_exits import parse_held_options
            opts = {h.symbol.upper(): h for h in parse_held_options(text or "")}
        except Exception:  # noqa: BLE001
            opts = {}
        net_liq = None
        if self.eng.portfolio is not None:
            try:
                bal = (await self.eng.portfolio.snapshot()).get("balance") or {}
                net_liq = _f(bal.get("net_liq"))
            except Exception:  # noqa: BLE001
                net_liq = None
        return {"last": last, "raw_qty": raw_qty, "options": opts, "net_liq": net_liq}

    async def _price(self, sym: str, snap: dict) -> float | None:
        p = snap["last"].get(sym)
        if p:
            return float(p)
        try:
            p = await self.broker.last_price(sym)
        except BrokerError:
            p = None
        if p:
            snap["last"][sym] = float(p)
        return _f(p)

    def _value(self, snap: dict) -> dict[str, dict]:
        """{sleeve: {equity, cash, invested}} plus 'total'. Swing = net liq minus the others."""
        out: dict[str, dict] = {}
        others = 0.0
        for sid in registry.NON_SWING:
            st = self.db.sleeve_state(sid)
            if not st:
                continue
            cash, invested = float(st.get("cash") or 0.0), 0.0
            for h in self.db.sleeve_holdings(sid):
                q = float(h["qty"])
                if h["kind"] == "option":
                    held = snap["options"].get(h["symbol"].upper())
                    mark = _f(getattr(held, "mark", None)) if held else None
                    invested += q * 100 * (mark if mark is not None else float(h.get("avg_cost") or 0))
                else:
                    px = snap["last"].get(h["symbol"]) or h.get("avg_cost") or 0.0
                    invested += q * float(px)
            out[sid] = {"equity": round(cash + invested, 2), "cash": round(cash, 2),
                        "invested": round(invested, 2)}
            others += cash + invested
        nl = snap.get("net_liq")
        if nl is not None:
            out["swing"] = {"equity": round(nl - others, 2), "cash": None, "invested": None}
            out["total"] = {"equity": round(nl, 2), "cash": None, "invested": None}
        return out

    def _record(self, values: dict, snap: dict) -> None:
        day = now_et().date().isoformat()
        for sid, v in values.items():
            self.db.record_sleeve_equity(day, sid, v["equity"], v.get("cash"), v.get("invested"))
        spy = snap["last"].get("SPY")
        if spy:
            self.db.record_sleeve_equity(day, "spy", float(spy))

    async def _drawdown_checks(self, values: dict) -> None:
        limit = float(self.s.sleeve_pause_dd_pct)
        for sid in ("swing", *registry.NON_SWING):
            v = values.get(sid)
            st = self.db.sleeve_state(sid)
            if not v or not st:
                continue
            eq = v["equity"]
            prev = self._last_eq.get(sid)
            self._last_eq[sid] = eq
            peak = float(st.get("peak_equity") or eq)
            # a new peak only from a believable reading (no >15% jump in one minute), so one bad
            # high balance read cannot set a peak that later looks like a drawdown
            if eq > peak and (prev is None or eq <= prev * 1.15):
                peak = eq
            if peak != st.get("peak_equity"):
                self.db.upsert_sleeve_state(sid, peak_equity=peak)
            dd = (eq / peak - 1) * 100 if peak > 0 else 0.0
            # one bad balance read must not pause a sleeve (the swing figure is net liq minus the
            # others): the breach has to hold for DD_CONFIRM_TICKS consecutive ticks (~minutes)
            hits = self._dd_hits.get(sid, 0) + 1 if dd <= limit else 0
            self._dd_hits[sid] = hits
            if hits >= DD_CONFIRM_TICKS and not st.get("paused"):
                await self.set_paused(sid, True, f"drawdown {dd:.1f}% reached the {limit:.0f}% pause line")

    def others_day_pnl(self) -> float:
        """Today's P&L of the non-swing sleeves (0 when sleeves are off or not valued yet)."""
        if not self.s.sleeves_enabled or not self.last_values:
            return 0.0
        today = now_et().date().isoformat()
        rows = [r for r in self.db.sleeve_equity_series(10) if r["day"] < today]
        prev = rows[-1] if rows else {}
        total = 0.0
        for sid in registry.NON_SWING:
            now_eq = (self.last_values.get(sid) or {}).get("equity")
            if now_eq is not None and prev.get(sid) is not None:
                total += now_eq - float(prev[sid])
        return round(total, 2)

    # ------------------------------------------------------------------ controls
    async def set_paused(self, sid: str, on: bool, reason: str) -> None:
        registry.get(sid)
        self.db.upsert_sleeve_state(sid, paused=on, pause_reason=reason if on else None)
        name = registry.get(sid).name
        self.db.log("warn" if on else "info",
                    f"Sleeve {name} {'PAUSED' if on else 'resumed'}: {reason}", alert=on)
        self._event(sid, "pause" if on else "resume", f"{name} {'paused' if on else 'resumed'}: {reason}")
        if sid == "swing":           # the swing sleeve is the engine: pause/resume its scheduler
            try:
                if on:
                    await self.eng.stop_scheduler()
                    self.eng.scheduler_stopped_by_user = True
                else:
                    self.eng.start_scheduler()
            except Exception:  # noqa: BLE001
                log.warning("could not change the swing scheduler", exc_info=True)

    async def rebalance_now(self, sid: str) -> str:
        if sid == "swing":
            raise ValueError("The swing sleeve trades continuously; there is nothing to rebalance.")
        registry.get(sid)
        st = self.db.sleeve_state(sid) or {}
        meta = _meta(st)
        meta.pop("targets", None)
        meta.pop("targets_month", None)
        meta["passes"] = 0
        self.db.upsert_sleeve_state(sid, last_rebalance=None, meta=meta)
        self._retry_after.pop(sid, None)
        self._last_tick = 0.0
        return "queued: the next minute's tick rebalances it (CORE session, after the start time)"

    # ------------------------------------------------------------------ orders
    async def _place_stock(self, sid: str, sym: str, side: str, qty: int, ref: float, why: str) -> int:
        """Send one marketable limit (already chunked). Returns the sleeve order id."""
        q = await self._quote(sym)
        bid, ask = (q.bid, q.ask) if q else (None, None)
        if side == "BUY":
            px = (ask or ref) * (1 + OFFSET_BPS / 1e4)
        else:
            px = (bid or ref) * (1 - OFFSET_BPS / 1e4)
        px = round(px, 2)
        coid = registry.coid_for(sid)
        oid = self.db.add_sleeve_order(sid, sym, "stock", side, qty, px, coid, reason=why)
        if side == "SELL":          # shares leave the sleeve when the sell is sent (see module doc)
            self._adjust_holding(sid, sym, -qty, None)
        try:
            await self.broker.place_order(sym, side, qty, px, coid, trading_session="CORE")
        except BrokerError as e:
            if e.transport:
                self.db.update_sleeve_order(oid, status="working", reason=f"{why}; no reply: {e}")
            else:
                self.db.update_sleeve_order(oid, status="rejected", reason=f"{why}; rejected: {e}")
                if side == "SELL":
                    self._adjust_holding(sid, sym, qty, None)
                self.db.log("warn", f"Sleeve {sid}: {side} {qty} {sym} rejected: {e}")
            return oid
        self.db.update_sleeve_order(oid, status="working")
        self.db.log("info", f"Sleeve {registry.get(sid).name}: {side} {qty} {sym} @ {px:.2f} ({why})")
        return oid

    async def _quote(self, sym: str):
        try:
            return await self.broker.quote(sym)
        except BrokerError:
            return None

    def _adjust_holding(self, sid: str, sym: str, dq: float, fill_px: float | None,
                        kind: str = "stock") -> None:
        cur = next((h for h in self.db.sleeve_holdings(sid) if h["symbol"] == sym.upper()), None)
        q0 = float(cur["qty"]) if cur else 0.0
        c0 = _f(cur.get("avg_cost")) if cur else None
        q1 = q0 + dq
        cost = c0
        if fill_px is not None and abs(q1) > abs(q0):        # adding to the position: blend the cost
            cost = ((abs(q0) * (c0 or fill_px)) + abs(dq) * fill_px) / abs(q1)
        self.db.set_sleeve_holding(sid, sym, kind, round(q1, 6), round(cost, 4) if cost else cost)

    async def sync_orders(self) -> None:
        """Settle working sleeve orders from the broker's open orders and history."""
        rows = self.db.sleeve_orders(None, 200, OPEN_STATUSES)
        if not rows:
            return
        try:
            seen = {o["client_order_id"]: o for o in parse_orders(await self.broker.order_history_text())}
            seen.update({o["client_order_id"]: o for o in parse_orders(await self.broker.open_orders_text())})
        except BrokerError as e:
            log.debug("sleeve sync: %s", e)
            return
        for r in rows:
            o = seen.get(r["client_order_id"])
            age = (now_utc() - datetime.fromisoformat(r["ts"])).total_seconds()
            if o is None:
                if age > GIVE_UP_SECONDS:
                    self._finish(r, "failed", "never seen at the broker")
                continue
            st = (o.get("status") or "").upper()
            filled = float(o.get("filled_qty") or 0)
            px = _f(o.get("avg_fill_price")) or _f(o.get("filled_price")) or _f(r.get("limit_price"))
            new = filled - float(r.get("filled_qty") or 0)
            if new > 1e-9 and px:
                self._apply_fill(r, new, px)
                self.db.update_sleeve_order(r["id"], filled_qty=filled, avg_fill_price=px)
                r = {**r, "filled_qty": filled}
            if st == "FILLED" or filled >= float(r["qty"]) - 1e-9:
                self.db.update_sleeve_order(r["id"], status="filled")
            elif st in ACTIVE or st.startswith(("PENDING", "PARTIAL", "WORKING", "SUBMITTED", "NEW")):
                if age > CANCEL_AFTER_SECONDS:
                    try:
                        await self.broker.cancel_order(r["client_order_id"])
                    except BrokerError as e:
                        log.debug("sleeve cancel %s: %s", r["client_order_id"], e)
                if filled > 0:
                    self.db.update_sleeve_order(r["id"], status="partial")
            else:                                # cancelled / rejected / expired / failed
                self._finish(r, "cancelled" if "CANCEL" in st else st.lower() or "failed",
                             f"broker status {st}")

    def _finish(self, r: dict, status: str, why: str) -> None:
        rest = float(r["qty"]) - float(r.get("filled_qty") or 0)
        if rest > 1e-9 and r["side"] == "SELL" and r["kind"] == "stock":
            self._adjust_holding(r["sleeve"], r["symbol"], rest, None)   # unsold shares come back
        self.db.update_sleeve_order(r["id"], status=status, reason=f"{r.get('reason') or ''}; {why}".strip("; "))

    def _apply_fill(self, r: dict, q: float, px: float) -> None:
        sid, sym = r["sleeve"], r["symbol"]
        if r["kind"] == "option":
            intent = (r.get("position_intent") or "").upper()
            if intent == "SELL_TO_OPEN":
                self._adjust_holding(sid, sym, -q, px, kind="option")
                self._add_cash(sid, q * 100 * px)
            else:                                   # BUY_TO_CLOSE
                self._adjust_holding(sid, sym, q, None, kind="option")
                self._add_cash(sid, -q * 100 * px)
            return
        if r["side"] == "BUY":
            self._adjust_holding(sid, sym, q, px)
            self._add_cash(sid, -q * px)
        else:                                       # shares were removed when the sell was sent
            self._add_cash(sid, q * px)

    # ------------------------------------------------------------------ momentum / VAA
    def _due_month(self, sid: str) -> bool:
        st = self.db.sleeve_state(sid) or {}
        last = st.get("last_rebalance")
        if not last:
            return True
        d = datetime.fromisoformat(last).date()
        today = now_et().date()
        return (d.year, d.month) != (today.year, today.month)

    async def _targets(self, sid: str) -> dict[str, float] | None:
        st = self.db.sleeve_state(sid) or {}
        meta = _meta(st)
        month = now_et().strftime("%Y-%m")
        if meta.get("targets_month") == month and meta.get("targets") is not None:
            return meta["targets"]
        if time.monotonic() < self._retry_after.get(sid, 0):
            return None
        from . import data
        if sid == "momentum":
            closes = await asyncio.to_thread(data.daily_closes, data.sp500_symbols())
            tg = momentum.targets(closes, n=int(self.s.sleeve_momentum_n))
        else:
            closes = await asyncio.to_thread(data.daily_closes, VAA_SYMBOLS)
            tg = vaa.targets(closes)
        if not tg:
            self._retry_after[sid] = time.monotonic() + 1800
            self.db.log("warn", f"Sleeve {sid}: could not compute targets (price history missing); "
                                "retrying in 30 min")
            return None
        meta.update({"targets": tg, "targets_month": month, "passes": 0,
                     "targets_at": now_utc().isoformat(timespec="seconds")})
        self.db.upsert_sleeve_state(sid, meta=meta)
        return tg

    async def _rebalance_step(self, sid: str, values: dict, snap: dict) -> None:
        st = self.db.sleeve_state(sid) or {}
        if st.get("paused") or not self._due_month(sid) or self.eng.kill_switch:
            return
        tg = await self._targets(sid)
        if tg is None:
            return
        meta = _meta(self.db.sleeve_state(sid))
        holdings = {h["symbol"]: float(h["qty"]) for h in self.db.sleeve_holdings(sid) if h["kind"] == "stock"}
        prices = {}
        for sym in set(tg) | set(holdings):
            p = await self._price(sym, snap)
            if p:
                prices[sym] = p
        equity = values.get(sid, {}).get("equity") or self._cash(sid)
        trades = rebalance.diff(holdings, tg, equity * 0.995, prices, self.s.sleeve_min_trade_usd)
        if not trades or meta.get("passes", 0) >= MAX_PASSES_PER_MONTH:
            if trades:
                self.db.log("warn", f"Sleeve {sid}: rebalance closed after {MAX_PASSES_PER_MONTH} passes "
                                    f"with {len(trades)} trade(s) left", alert=True)
            self.db.upsert_sleeve_state(sid, last_rebalance=now_utc().isoformat(timespec="seconds"))
            top = ", ".join(f"{k} {v:.0%}" for k, v in sorted(tg.items(), key=lambda kv: -kv[1]))
            self.db.log("info", f"Sleeve {registry.get(sid).name}: rebalanced to " + top)
            self._event(sid, "rebalance", f"Rebalanced to {top}"[:200])
            return
        meta["passes"] = meta.get("passes", 0) + 1
        self.db.upsert_sleeve_state(sid, meta=meta)
        sells = [t for t in trades if t[1] == "SELL"]
        if sells:                               # sells first; buys in a later pass, from the cash raised
            for sym, side, qty in sells:
                await self._send_chunks(sid, sym, side, qty, prices[sym], "rebalance")
            return
        cash = self._cash(sid)
        need = sum(q * prices[s] * (1 + OFFSET_BPS / 1e4) for s, _, q in trades)
        scale = min(1.0, (cash * 0.995) / need) if need > 0 else 0.0
        for sym, side, qty in trades:
            q = int(math.floor(qty * scale))
            if q >= 1 and q * prices[sym] >= min(self.s.sleeve_min_trade_usd, cash):
                await self._send_chunks(sid, sym, side, q, prices[sym], "rebalance")

    async def _send_chunks(self, sid: str, sym: str, side: str, qty: int, price: float, why: str) -> None:
        s = self.s
        cap_usd = min([c for c in (s.max_order_usd, getattr(s, "max_order_notional", 0)) if c] or [0])
        unit = price * (1 + 2 * OFFSET_BPS / 1e4)
        if side == "BUY" and cap_usd and unit > cap_usd:
            # one share already breaks MAX_ORDER_USD: the guard would refuse every piece
            self.db.log("warn", f"Sleeve {sid}: cannot buy {sym} at ~${unit:,.2f}/share with MAX_ORDER_USD="
                                f"{cap_usd:,.0f}; raise it before enabling sleeves", alert=True)
            return
        for q in rebalance.chunks(int(qty), unit, cap_usd, s.max_order_qty):
            await self._place_stock(sid, sym, side, int(q), price, why)

    # ------------------------------------------------------------------ put-write
    async def _putwrite_step(self, values: dict, snap: dict) -> None:
        sid, s = "putwrite", self.s
        today, now = now_et().date(), now_et()
        under = s.sleeve_putwrite_underlying
        holds = self.db.sleeve_holdings(sid)
        # 1. assigned or leftover shares are sold (≈ the cash settlement the backtest models)
        for h in [h for h in holds if h["kind"] == "stock" and float(h["qty"]) > 0]:
            px = await self._price(h["symbol"], snap) or float(h.get("avg_cost") or 0)
            await self._send_chunks(sid, h["symbol"], "SELL", int(float(h["qty"])), px, "assigned shares")
            return
        puts = [h for h in holds if h["kind"] == "option" and float(h["qty"]) < 0]
        spot = await self._price(under, snap)
        for h in puts:
            occ = parse_occ(h["symbol"]) or {}
            if not occ:
                continue
            put = {"strike": occ["strike"], "expiration": date.fromisoformat(occ["expiration"])}
            if h["symbol"].upper() not in snap["options"] and putwrite.is_expired(put, today, now):
                self._settle_expired(h, occ, snap)
                return
            if spot and putwrite.should_close(put, spot, today, now):
                await self._close_put(h, occ)
                return
        if puts:
            return
        st = self.db.sleeve_state(sid) or {}
        if st.get("paused") or self.eng.kill_switch or _meta(st).get("cycle_end_day") == today.isoformat():
            return
        await self._open_put(under, spot, today)

    def _settle_expired(self, h: dict, occ: dict, snap: dict) -> None:
        """The put left the account: expired worthless, or assigned (shares appeared)."""
        sid, under, n = "putwrite", occ["underlying"], abs(float(h["qty"]))
        raw = float(snap["raw_qty"].get(under, 0.0))
        reserved = self.db.sleeve_reserved().get(under, 0.0)
        swing_open = sum(float(t.get("quantity_filled_real") or t.get("quantity_filled") or 0)
                         for t in self.db.open_trades() if t.get("symbol") == under and t.get("side") == "BUY")
        extra = raw - reserved - swing_open
        self.db.set_sleeve_holding(sid, h["symbol"], "option", 0, None)
        meta = _meta(self.db.sleeve_state(sid))
        meta["cycle_end_day"] = now_et().date().isoformat()
        self.db.upsert_sleeve_state(sid, meta=meta)
        if extra >= n * 100 * 0.99:
            shares = n * 100
            self.db.set_sleeve_holding(sid, under, "stock", shares, occ["strike"])
            self._add_cash(sid, -shares * occ["strike"])
            self.db.log("warn", f"Sleeve put-write: {h['symbol']} was ASSIGNED; {shares:g} {under} booked "
                                f"at {occ['strike']} and will be sold", alert=True)
            self._event(sid, "assigned", f"Put {h['symbol']} assigned: {shares:g} {under} at {occ['strike']}")
        else:
            self.db.log("info", f"Sleeve put-write: {h['symbol']} expired; premium kept")
            self._event(sid, "put_expired", f"Put {h['symbol']} expired worthless; premium kept")

    async def _close_put(self, h: dict, occ: dict) -> None:
        sym, n = h["symbol"], int(abs(float(h["qty"])))
        q = await self._option_quote(sym)
        ask = (q or {}).get("ask") or (q or {}).get("last")
        if not ask:
            self.db.log("warn", f"Sleeve put-write: no quote to buy back {sym}; retrying")
            return
        px = round(float(ask) * 1.02 + 0.01, 2)
        await self._send_option(sym, occ, "BUY", "BUY_TO_CLOSE", n, px, "in the money at expiry: buy back")
        meta = _meta(self.db.sleeve_state("putwrite"))
        meta["cycle_end_day"] = now_et().date().isoformat()
        self.db.upsert_sleeve_state("putwrite", meta=meta)

    async def _option_quote(self, sym: str) -> dict | None:
        try:
            return (await self.broker.parsed_option_snapshots([sym])).get(sym)
        except BrokerError:
            return None

    async def _open_put(self, under: str, spot: float | None, today: date) -> None:
        s = self.s
        if not spot:
            return
        text = await self.broker.option_contracts([under])
        rows = []
        for tok in dict.fromkeys(_OCC_TOKEN.findall(text or "")):
            occ = parse_occ(tok)
            if not occ or occ["underlying"] != under or occ["option_type"] != "PUT":
                continue
            exp = date.fromisoformat(occ["expiration"])
            # standard strikes only: adjusted contracts (after a corporate action) carry odd
            # strikes such as 579.78 and trade thinly (2026-11-25 the picker chose them)
            if abs(occ["strike"] * 2 - round(occ["strike"] * 2)) > 1e-6:
                continue
            if 21 <= (exp - today).days <= 45:
                rows.append({**occ, "symbol": tok, "expiration": exp})
        if not rows:
            self.db.log("warn", f"Sleeve put-write: no {under} put expiring in 21-45 days")
            return
        target = min({r["expiration"] for r in rows}, key=lambda e: abs((e - today).days - s.sleeve_putwrite_dte))
        dte = (target - today).days
        z = NormalDist().inv_cdf(1 - s.sleeve_putwrite_delta)
        est = spot * math.exp(-z * 0.25 * math.sqrt(max(dte, 1) / 365))
        near = sorted((r for r in rows if r["expiration"] == target), key=lambda r: abs(r["strike"] - est))[:10]
        snaps = await self.broker.parsed_option_snapshots([r["symbol"] for r in near])
        chain = []
        for r in near:
            d = snaps.get(r["symbol"]) or {}
            chain.append({**r, "delta": d.get("delta"), "bid": d.get("bid"), "ask": d.get("ask")})
        pick = putwrite.pick_contract(chain, spot, today, s.sleeve_putwrite_delta, s.sleeve_putwrite_dte)
        if not pick:
            self.db.log("warn", f"Sleeve put-write: no {under} {target} put with a two-sided quote")
            return
        n = putwrite.contracts_for(self._cash("putwrite"), pick["strike"])
        if n < 1:
            self.db.log("warn", "Sleeve put-write: cash does not secure one contract")
            return
        bid, ask = float(pick["bid"]), float(pick.get("ask") or pick["bid"])
        px = round(max(bid, (bid + ask) / 2 - 0.25 * (ask - bid)), 2)
        occ = parse_occ(pick["symbol"])
        await self._send_option(pick["symbol"], occ, "SELL", "SELL_TO_OPEN", n, px,
                                f"sell {n} x {pick['symbol']} (delta {pick.get('delta')}, {pick['dte']} DTE)")

    async def _send_option(self, sym: str, occ: dict, side: str, intent: str, n: int, px: float, why: str) -> None:
        s = self.s
        cap_usd = min([c for c in (s.max_order_usd, getattr(s, "max_order_notional", 0)) if c] or [0])
        closing = intent.endswith("_TO_CLOSE")
        if not closing and cap_usd and px * 100 > cap_usd:
            self.db.log("warn", f"Sleeve put-write: one contract (${px * 100:,.0f}) exceeds MAX_ORDER_USD="
                                f"{cap_usd:,.0f}; raise it before enabling sleeves", alert=True)
            return
        pieces = rebalance.chunks(n, px * 100, cap_usd, int(s.options_max_contracts_per_order or 0))
        for q in pieces:
            coid = registry.coid_for("putwrite")
            oid = self.db.add_sleeve_order("putwrite", sym, "option", side, q, px, coid,
                                           position_intent=intent, reason=why)
            try:
                await self.broker.place_option_single_order(
                    sym, side, int(q), occ["option_type"], occ["strike"], occ["expiration"], px, coid,
                    position_intent=intent, allow_when_killed=closing)
            except BrokerError as e:
                if e.transport:
                    self.db.update_sleeve_order(oid, status="working", reason=f"{why}; no reply: {e}")
                else:
                    self.db.update_sleeve_order(oid, status="rejected", reason=f"{why}; rejected: {e}")
                    self.db.log("warn", f"Sleeve put-write: {intent} {q} {sym} rejected: {e}", alert=True)
                return
            self.db.update_sleeve_order(oid, status="working")
            self.db.log("info", f"Sleeve put-write: {intent} {q} {sym} @ {px:.2f} ({why})")
            if intent in ("SELL_TO_OPEN", "BUY_TO_CLOSE"):
                self._event("putwrite", "put_open" if intent == "SELL_TO_OPEN" else "put_close",
                            f"{'Sold' if intent == 'SELL_TO_OPEN' else 'Bought back'} {q} x {sym} @ {px:.2f}")

    # ------------------------------------------------------------------ API views
    def _sleeve_view(self, d: registry.SleeveDef, values: dict) -> dict:
        s = self.s
        st = self.db.sleeve_state(d.id) or {}
        v = values.get(d.id) or {}
        eq = v.get("equity")
        enabled = s.sleeves_enabled and (d.id == "swing" or d.id in self.enabled_ids())
        start = float(st.get("start_capital") or (s.sleeve_start_capital if d.id != "swing" else 0) or 0)
        series = [r for r in self.db.sleeve_equity_series(400) if r.get(d.id) is not None]
        if d.id == "swing" and not start and series:
            start = float(series[0][d.id])
        prev = next((r[d.id] for r in reversed(series) if r["day"] < now_et().date().isoformat()), None)
        peak = _f(st.get("peak_equity")) or eq
        rets = []
        for a, b in zip(series, series[1:]):
            if a[d.id]:
                rets.append(b[d.id] / a[d.id] - 1)
        sharpe = vol = None
        if len(rets) >= 20:
            m = sum(rets) / len(rets)
            sd = (sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
            vol = sd * math.sqrt(252) * 100
            sharpe = (m / sd * math.sqrt(252)) if sd > 0 else None
        holds = self.db.sleeve_holdings(d.id) if d.id != "swing" else []
        status = "disabled" if not enabled else ("paused" if st.get("paused") else "active")
        reason = st.get("pause_reason") if st.get("paused") else None
        if d.id != "swing" and enabled and not st.get("paused"):
            if self.db.sleeve_orders(d.id, 5, OPEN_STATUSES):
                status, reason = "active", "orders working"
            elif not holds:
                status, reason = "waiting", "waiting for the first trade (CORE session, after " \
                                            f"{s.sleeve_rebalance_after_et} ET)"
        last_rb = st.get("last_rebalance")
        nxt = None
        if d.id in ("momentum", "vaa"):
            base = datetime.fromisoformat(last_rb).date() if last_rb else None
            nxt = (next_rebalance_day(base).isoformat() if base else now_et().date().isoformat())
        elif d.id == "putwrite":
            put = next((h for h in holds if h["kind"] == "option"), None)
            occ = parse_occ(put["symbol"]) if put else None
            nxt = occ["expiration"] if occ else now_et().date().isoformat()
        invested = v.get("invested")
        if d.id == "putwrite":      # what is committed is the cash securing the puts, not their mark
            invested = round(sum(abs(float(h["qty"])) * 100 * (parse_occ(h["symbol"]) or {}).get("strike", 0)
                                 for h in holds if h["kind"] == "option"), 2) or (invested if holds else 0.0)
        if d.id == "swing" and self.eng.portfolio is not None and self.eng.portfolio._cache:
            invested = sum(float(r.get("market_value") or 0) for r in self.eng.portfolio._cache.get("positions", []))
        return {
            "id": d.id, "name": d.name, "color_slot": d.color_slot, "description": d.description,
            "rebalance_rule": d.rebalance_rule, "expected": d.expected,
            "status": status, "status_reason": reason,
            "start_capital": start or None, "start_date": st.get("start_date"),
            "equity": eq, "cash": v.get("cash"), "invested": invested,
            "invested_pct": round(invested / eq * 100, 1) if eq and invested is not None else None,
            "day_pnl": round(eq - prev, 2) if eq is not None and prev else None,
            "day_pnl_pct": round((eq / prev - 1) * 100, 2) if eq is not None and prev else None,
            "total_return_pct": round((eq / start - 1) * 100, 2) if eq and start else None,
            "peak_equity": peak, "drawdown_pct": round((eq / peak - 1) * 100, 2) if eq and peak else None,
            "pause_dd_pct": s.sleeve_pause_dd_pct,
            "positions_n": (len(holds) if d.id != "swing" else
                            len((self.eng.portfolio._cache or {}).get("positions", []))
                            if self.eng.portfolio is not None else None),
            "last_rebalance": last_rb, "next_rebalance": nxt,
            "live": {"days": len(rets), "sharpe": round(sharpe, 2) if sharpe is not None else None,
                     "vol": round(vol, 1) if vol is not None else None},
        }

    async def summary(self) -> dict:
        values = self.last_values
        if self.s.sleeves_enabled and not values:
            try:
                snap = await self._snapshot()
                values = self.last_values = self._value(snap)
            except Exception:  # noqa: BLE001
                values = {}
        total = values.get("total", {}).get("equity")
        sleeves = [self._sleeve_view(d, values) for d in registry.SLEEVES]
        short = self.capital_shortfall(total) if self.s.sleeves_enabled else None
        if short:
            for x in sleeves:
                x.update(status="disabled", status_reason=short)
                if x["id"] == "swing":
                    x.update(equity=total, start_capital=None, peak_equity=None, drawdown_pct=None,
                             total_return_pct=None, invested_pct=None)
        return {
            "enabled": bool(self.s.sleeves_enabled), "as_of": now_utc().isoformat(timespec="seconds"),
            "benchmark": "SPY", "last_error": short or self.last_error,
            "account": {"net_liq": total, "target_per_sleeve": self.s.sleeve_start_capital,
                        "sleeves_equity": round(sum(x["equity"] or 0 for x in sleeves), 2),
                        "day_pnl": (round(sum(x["day_pnl"] for x in sleeves if x["day_pnl"] is not None), 2)
                                    if any(x["day_pnl"] is not None for x in sleeves) else None)},
            "sleeves": sleeves,
        }

    async def detail(self, sid: str) -> dict:
        d = registry.get(sid)
        base = self._sleeve_view(d, self.last_values)
        eq = base.get("equity") or 0
        st = self.db.sleeve_state(sid) or {}
        tg = _meta(st).get("targets") or {}
        holdings = []
        if sid == "swing" and self.eng.portfolio is not None:
            for r in (self.eng.portfolio._cache or {}).get("positions", []):
                holdings.append({"symbol": r["symbol"], "kind": "stock", "qty": r["qty"], "avg_cost": r.get("cost"),
                                 "last": r.get("last"), "market_value": r.get("market_value"),
                                 "weight_pct": round(float(r.get("market_value") or 0) / eq * 100, 2) if eq else None,
                                 "target_weight_pct": None, "unrealized_pnl": r.get("unrealized"),
                                 "unrealized_pct": r.get("unrealized_pct"), "note": None})
        else:
            text = await self.broker.positions_text()
            rows = {r["symbol"]: r for r in parse_position_rows(text or "")}
            try:
                from ..engine.option_exits import parse_held_options
                opts = {h.symbol.upper(): h for h in parse_held_options(text or "")}
            except Exception:  # noqa: BLE001
                opts = {}
            for h in self.db.sleeve_holdings(sid):
                q, cost = float(h["qty"]), _f(h.get("avg_cost"))
                if h["kind"] == "option":
                    o = opts.get(h["symbol"].upper())
                    last = _f(getattr(o, "mark", None)) if o else None
                    mv = q * 100 * (last if last is not None else (cost or 0))
                    upl = (cost - last) * abs(q) * 100 if (cost is not None and last is not None) else None
                    note = "short put (premium received)"
                else:
                    last = _f(rows.get(h["symbol"], {}).get("last"))
                    mv = q * (last if last is not None else (cost or 0))
                    upl = (last - cost) * q if (cost and last) else None
                    note = None
                holdings.append({"symbol": h["symbol"], "kind": h["kind"], "qty": q, "avg_cost": cost, "last": last,
                                 "market_value": round(mv, 2),
                                 "weight_pct": round(mv / eq * 100, 2) if eq else None,
                                 "target_weight_pct": round(tg[h["symbol"]] * 100, 2) if h["symbol"] in tg else None,
                                 "unrealized_pnl": round(upl, 2) if upl is not None else None,
                                 "unrealized_pct": (round(upl / abs(cost * q * (100 if h["kind"] == "option" else 1))
                                                          * 100, 2) if upl is not None and cost else None),
                                 "note": note})
        orders = [{"ts": o["ts"], "symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                   "limit_price": o["limit_price"], "status": o["status"], "filled_qty": o["filled_qty"],
                   "avg_fill_price": o["avg_fill_price"], "reason": o["reason"]}
                  for o in self.db.sleeve_orders(sid, 20)]
        month = _meta(st).get("targets_month")
        targets = [{"symbol": k, "weight_pct": round(v * 100, 2), "reason": f"ranked for {month}" if month else None}
                   for k, v in sorted(tg.items(), key=lambda kv: -kv[1])]
        return {**base, "holdings": holdings, "targets": targets, "orders": orders}

    def equity_series(self, days: int = 365) -> dict:
<<<<<<< HEAD
        """Daily equity per sleeve plus 'total' and 'spy', with a start baseline row.

        The baseline row (``baseline: True``) sits on the last stored close before the
        sleeves' common start date: each sleeve at its start capital, total = that
        close's net liq, swing = net liq minus the other sleeves' capital, spy = that
        day's SPY close. Every line is then indexed from the same starting point.
        """
        series = self.db.sleeve_equity_series(days)
        start = self.common_start_date()
        if not start:
            return {"series": series, "start_date": None}
        series = [r for r in series if r["day"] >= start]
        base = self._baseline_row(start)
        return {"series": ([base] if base else []) + series, "start_date": start}

    def common_start_date(self) -> str | None:
        """Earliest start date among the live sleeves (they all go live together)."""
        dates = [st.get("start_date") for sid in ("swing", *self.enabled_ids())
                 if (st := self.db.sleeve_state(sid)) and st.get("start_date")]
        return min(dates) if dates else None

    def _baseline_row(self, start: str) -> dict | None:
        try:
            prev = [r for r in self.db.benchmark_daily() if r["day"] < start
                    and r.get("net_liq") is not None and r.get("spy_close") is not None]
        except Exception:  # noqa: BLE001 - no baseline, lines index from their first day
            prev = []
        others = {}
        for sid in self.enabled_ids():
            st = self.db.sleeve_state(sid) or {}
            cap = _f(st.get("start_capital"))
            if cap:
                others[sid] = cap
        if not prev:
            if not others:
                return None
            day = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
            return {"day": day, "baseline": True, **others}
        last = prev[-1]
        nl = float(last["net_liq"])
        return {"day": last["day"], "baseline": True, **others,
                "swing": round(nl - sum(others.values()), 2), "total": round(nl, 2),
                "spy": float(last["spy_close"])}
=======
        return {"series": self.db.sleeve_equity_series(days)}
>>>>>>> 2e32c5e54414a1984ef1cfefe57d83289c20ffb5
