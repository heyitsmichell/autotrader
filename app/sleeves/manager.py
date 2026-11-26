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

from ._common import *  # noqa: F401,F403  (re-exports every name importers rely on)
from .orders import OrdersMixin
from .putwrite_ops import PutWriteMixin
from .rebalance_ops import RebalanceMixin
from .valuation import ValuationMixin
from .views import ViewsMixin


class SleeveManager(ValuationMixin, OrdersMixin, RebalanceMixin, PutWriteMixin, ViewsMixin):
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
            self.broker.sleeve_day_pnl_provider = self.others_day_pnl
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
        try:
            await self._price("SPY", snap)    # SPY is rarely held, so the snapshot lacks it
        except Exception as exc:  # noqa: BLE001 - the benchmark line is optional, never block the tick
            log.debug("SPY benchmark price failed: %s", exc)
            pass
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

    async def summary(self) -> dict:
        values = self.last_values
        if self.s.sleeves_enabled and not values:
            try:
                snap = await self._snapshot()
                values = self.last_values = self._value(snap)
            except Exception as exc:  # noqa: BLE001
                log.warning("sleeve snapshot failed: %s", exc)
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
            except Exception as exc:  # noqa: BLE001
                log.warning("held-option parse failed: %s", exc)
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
        except Exception as exc:  # noqa: BLE001 - no baseline, lines index from their first day
            log.debug("benchmark baseline unavailable: %s", exc)
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
