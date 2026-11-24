"""RiskLoop: a short-interval task that protects what is held (QA section 3.3, H1).

Every ``risk_loop_seconds`` (never below 0.25) it runs the OptionExitManager over the held option
positions. It is intentionally boring:

  * it awaits nothing except broker calls (positions, option snapshots, order placement), so a
    slow research/LLM/yfinance call can never delay an exit;
  * it never takes ``_run_lock`` (the slow cycle may hold it for a long time) nor ``_exec_lock``;
    the only engine lock it uses is ``_proposal_lock``, briefly, to insert an exit row;
  * every iteration is wrapped in try/except: an exception is logged and counted, never fatal;
  * it runs whatever the kill switch, approve/auto mode, ``OPTIONS_ENABLED`` and scheduler state
    are. Exits are never gated.

Equity signal exits (``exits.decide_exit``: time stop, trend break, ML drop, TA exit bar,
trailing stop) are NOT moved here. They need the proposal path (eligibility checks that call the
broker and yfinance, approve-mode review, per-run caps) and daily-bar signals that only change
every few minutes, so they stay in the slow cycle. Equity positions are protected between cycles
by their broker-side bracket/stop orders. This is deliberate and documented in the report.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..broker import BrokerError
from ..market import order_session
from ..portfolio import ACTIVE, DONE_CANCELLED, parse_orders

from .chase import EntryChaser
from .option_exits import OptionExitManager
from .util import _coid, _is_option_symbol
from .workers import Workers

log = logging.getLogger("app.engine")

MIN_INTERVAL_SECONDS = 0.25
SOFT_STOP_OFFSET_PCT = 0.005     # soft-stop limit sits this far below the bid so it is marketable
SOFT_STOP_RETRY_SECONDS = 15.0   # after a definite broker rejection
SOFT_STOP_CONFIRM_SECONDS = 30.0  # S6: readings below the stop must span at least this long
SOFT_STOP_MIN_READINGS = 2
POSITIONS_TTL_SECONDS = 5.0      # positions() is not called every second by the soft-stop loop
IDLE_INTERVAL_SECONDS = 30.0     # polling cadence while no option is held and options are off


class RiskLoop:
    def __init__(self, engine) -> None:
        self.eng = engine
        self._task: asyncio.Task | None = None
        self.ticks = 0
        self.errors = 0
        self.exits = 0
        self.last_tick_at: float | None = None
        self.last_error: str | None = None
        self._last_poll = 0.0
        self.soft_stops = 0
        self._soft_retry_at: dict[int, float] = {}
        self._clock = time.monotonic             # tests replace it
        self._below: dict[str, dict] = {}        # symbol -> {first, n}: readings below the stop
        self._pos_cache: tuple[float, dict | None] = (0.0, None)
        self._followup_at = -1e9
        self.chaser = EntryChaser(engine)       # market-orders: ENTRY_CHASE_SECONDS

    @property
    def interval(self) -> float:
        try:
            return max(MIN_INTERVAL_SECONDS, float(self.eng.s.risk_loop_seconds))
        except (TypeError, ValueError, AttributeError):
            return 1.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name="risk-loop")
            self.eng.db.log("info", f"Risk loop started: every {self.interval:g}s "
                                    "(option exits; equity exits stay in the slow cycle)")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.eng.db.log("info", "Risk loop stopped")

    def status(self) -> dict:
        return {"running": self.running, "interval_seconds": self.interval, "ticks": self.ticks,
                "errors": self.errors, "exits": self.exits, "last_error": self.last_error,
                "last_tick_at": self.last_tick_at,
                "held_options": sorted(self.eng.option_exits.held)}

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the loop must survive everything
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("risk loop iteration failed")
            await asyncio.sleep(self.interval)

    async def tick(self) -> list[dict]:
        """One iteration (also called directly by tests). Raises only what the caller should
        see; ``_loop`` catches it."""
        self.ticks += 1
        self.last_tick_at = time.time()
        eng = self.eng
        if not eng.broker.connected:
            return []
        try:
            await self._soft_stops()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never let the equity watch block option exits
            self.errors += 1
            self.last_error = f"soft stop: {type(e).__name__}: {e}"
            log.exception("extended-hours soft stop check failed")
        try:
            await self.chaser.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a chase problem must never block exits
            self.errors += 1
            self.last_error = f"entry chase: {type(e).__name__}: {e}"
            log.exception("entry chase failed")
        manager = eng.option_exits
        idle = not manager.held and not eng.s.options_enabled
        if idle and time.monotonic() - self._last_poll < IDLE_INTERVAL_SECONDS:
            return []
        self._last_poll = time.monotonic()
        actions = await manager.run_once()
        self.exits += sum(1 for a in actions if a)
        self.last_error = manager.last_error
        return actions


    async def _positions(self) -> dict:
        """positions() throttled to POSITIONS_TTL_SECONDS: the soft-stop loop runs every second."""
        now = self._clock()
        at, pos = self._pos_cache
        if pos is not None and now - at < POSITIONS_TTL_SECONDS:
            return pos
        pos = await self.eng.broker.positions()
        self._pos_cache = (now, pos)
        return pos

    async def _foreign_working_sell(self, sym: str) -> bool:
        """A working SELL for the symbol that is neither one of our stop/target orders (those the
        soft stop cancels itself) nor unreadable. Soft-stop and exit sells count as foreign here,
        so a second sell is never stacked on them."""
        eng = self.eng
        try:
            orders = parse_orders(await eng.broker.open_orders_text())
        except Exception:  # noqa: BLE001 - cannot tell: protection matters more than the check
            return False
        ours = {r["client_order_id"] for r in eng.db.protective(sym)
                if not (r.get("note") or "").startswith(("extended-hours soft stop", "exit:"))}
        for o in orders:
            st = (o.get("status") or "").upper()
            if ((o.get("symbol") or "").upper() == sym.upper()
                    and (o.get("side") or "").upper() == "SELL"
                    and (st in ACTIVE or st.startswith(("PENDING", "PARTIAL")))
                    and o.get("client_order_id") not in ours):
                return True
        return False

    async def _soft_stops(self) -> list[int]:
        """Extended-hours soft stop for held equities.

        The broker stop is a CORE order: it only becomes active at 09:30 ET. In premarket and
        after-hours (and overnight, for OVERNIGHT_SYMBOLS) watch the bid (last when there is no
        bid) and, once it is at or below the trade's stop price, cancel the resting stop and send
        a marketable LIMIT SELL in the current session. Needs a fresh, non-stale quote and at
        least two readings below the stop >= SOFT_STOP_CONFIRM_SECONDS apart (reset when the
        price recovers). One sell per SYMBOL (sized to the held quantity), skipped when another
        SELL is already working. Never blocked by the kill switch (a closing order). Returns the
        trade ids acted on."""
        eng = self.eng
        if not getattr(eng.s, "extended_hours_soft_stop", False):
            return []
        session = order_session()
        if session in ("ALL", "NIGHT", "CORE"):
            await self._soft_followups(session)
        if session not in ("ALL", "NIGHT"):
            return []                       # CORE: the broker stop is live; None: market closed
        trades = [t for t in eng.db.open_trades()
                  if t.get("side") == "BUY" and t.get("stop_price")
                  and t.get("entry_fill_price") is not None and t.get("exit_fill_price") is None]
        if not trades:
            return []
        now = self._clock()
        by_sym: dict[str, list[dict]] = {}
        for t in trades:
            if eng.db.get_kv(f"soft_stop:{t['id']}") or self._soft_retry_at.get(t["id"], 0.0) > now:
                continue
            sym = t["symbol"]
            if _is_option_symbol(sym):
                continue
            if session == "NIGHT" and not (eng.s.overnight_trading
                                           and sym in eng.s.overnight_symbols):
                continue
            by_sym.setdefault(sym, []).append(t)
        if not by_sym:
            return []
        positions = await self._positions()
        fired: list[int] = []
        for sym, group in by_sym.items():
            held = positions.get(sym, 0.0)
            if held <= 0:
                self._below.pop(sym, None)
                continue
            q = await eng.broker.quote(sym)
            if (q is None or q.stale
                    or not q.is_fresh(float(eng.s.equity_quote_max_age_seconds))):
                continue
            bid, ask = q.bid, q.ask
            if bid and ask and not q.two_sided:
                continue                    # crossed/locked quote: not trustworthy
            ref = bid if bid and bid > 0 else q.last
            t = max(group, key=lambda x: float(x["stop_price"]))    # the first stop to be hit
            stop = float(t["stop_price"])
            if not ref or ref <= 0 or ref > stop:
                self._below.pop(sym, None)  # recovered: start counting again
                continue
            st = self._below.get(sym)
            if st is None:
                self._below[sym] = {"first": now, "n": 1}
                continue
            st["n"] += 1
            if st["n"] < SOFT_STOP_MIN_READINGS or now - st["first"] < SOFT_STOP_CONFIRM_SECONDS:
                continue
            if await self._foreign_working_sell(sym):
                continue                    # a SELL is already working: do not stack another
            if await self._fire_soft_stop(t, held, ref, stop, session, group):
                fired.append(t["id"])
                self._below.pop(sym, None)
        return fired

    async def _fire_soft_stop(self, t: dict, held: float, ref: float, stop: float,
                              session: str, group: list[dict] | None = None) -> bool:
        eng, sym, tid = self.eng, t["symbol"], t["id"]
        coid = _coid()
        limit = max(0.01, round(ref * (1 - SOFT_STOP_OFFSET_PCT), 2))
        marker = {"coid": coid, "at": time.time()}
        ids = [x["id"] for x in (group or [t])]
        for i in ids:                       # once per symbol: every trade in it is covered
            eng.db.set_kv(f"soft_stop:{i}", marker)
        stops = await eng._cancel_protective(sym, coid)
        if stops is None:                   # cancel got no reply: do not sell; retry shortly
            for i in ids:
                eng.db.set_kv(f"soft_stop:{i}", None)
            self._soft_retry_at[tid] = self._clock() + SOFT_STOP_RETRY_SECONDS
            return False
        row = eng.db.add_protective(t["proposal_id"], sym, "sl", coid, held, stop, "unknown",
                                    f"extended-hours soft stop: {session} limit sell")
        try:
            await eng.broker.place_order(sym, "SELL", held, limit, coid, trading_session=session,
                                         allow_when_killed=True)
        except BrokerError as e:
            if e.transport:                 # outcome unknown: keep the guard, let the sync settle it
                eng.db.log("error", f"{sym}: soft-stop sell got no reply ({e}). Check Webull.",
                           alert=True)
                return True
            eng.db.set_protective(row, "closed", f"soft stop sell rejected: {e}")
            for i in ids:
                eng.db.set_kv(f"soft_stop:{i}", None)
            self._soft_retry_at[tid] = self._clock() + SOFT_STOP_RETRY_SECONDS
            eng.db.log("error", f"{sym}: soft-stop sell rejected ({e}); the stop is restored if "
                                "the periodic check finds it missing. Retrying shortly.", alert=True)
            return False
        eng.db.set_protective(row, "active", f"extended-hours soft stop: {session} limit sell")
        self.soft_stops += 1
        eng.db.log("warn", f"SOFT STOP {sym}: price {ref:.2f} <= stop {stop:.2f} in the {session} "
                           f"session; resting stop cancelled, LIMIT SELL {held:g} @ {limit:.2f} sent.",
                   alert=True)
        return True

    # -- S5: a soft-stop sell that does not fill must not leave the position bare ----------
    async def _soft_followups(self, session: str) -> None:
        """For each fired soft stop that is not settled: at the CORE open an unfilled sell is
        cancelled; once it ends unfilled (cancelled/expired/rejected) the CORE stop is restored
        at the ledger stop, or the position is exited when the price is at/below that stop."""
        eng = self.eng
        by_coid: dict[str, tuple[dict, list[dict], dict]] = {}
        for t in eng.db.open_trades():
            v = eng.db.get_kv(f"soft_stop:{t['id']}")
            if (isinstance(v, dict) and v.get("coid") and not v.get("settled")
                    and t.get("exit_fill_price") is None):
                by_coid.setdefault(v["coid"], (t, [], v))[1].append(t)
        if not by_coid:
            return
        now = self._clock()
        if now - self._followup_at < POSITIONS_TTL_SECONDS:
            return
        self._followup_at = now
        try:
            open_ = {o["client_order_id"]: o for o in parse_orders(await eng.broker.open_orders_text())}
            hist = {o["client_order_id"]: o for o in parse_orders(await eng.broker.order_history_text())}
            positions = await self._positions()
        except BrokerError:
            return

        def settle(group: list[dict], v: dict, **extra) -> None:
            for x in group:
                eng.db.set_kv(f"soft_stop:{x['id']}", {**v, "settled": True, **extra})

        for coid, (t, group, v) in by_coid.items():
            sym = t["symbol"]
            held = positions.get(sym, 0.0)
            o = open_.get(coid) or hist.get(coid)
            status = ((o or {}).get("status") or "").upper()
            if held <= 0 or status == "FILLED":
                settle(group, v)
                continue
            if o is None:
                if time.time() - float(v.get("at", 0)) > 600:
                    settle(group, v)
                    await self._restore_after_soft(t, held, session)
                continue
            if status in DONE_CANCELLED:
                settle(group, v)
                await self._restore_after_soft(t, held, session)
            elif session == "CORE" and not v.get("cancelling"):
                try:
                    await eng.broker.cancel_order(coid)
                except BrokerError as e:
                    eng.db.log("error", f"{sym}: could not cancel the unfilled soft-stop sell at the "
                                        f"open ({e}). Check Webull.", alert=True)
                    continue
                for x in group:
                    eng.db.set_kv(f"soft_stop:{x['id']}", {**v, "cancelling": True})
                eng.db.log("warn", f"{sym}: the soft-stop sell is still unfilled at the open; "
                                   "cancelled. The stop is restored (or the position exited) once "
                                   "the broker confirms.", alert=True)

    async def _restore_after_soft(self, t: dict, held: float, session: str) -> None:
        """Put the CORE stop back at the ledger stop, or exit when the price is at/below it."""
        eng, pf, sym = self.eng, self.eng.portfolio, t["symbol"]
        if pf is None or held <= 0:
            return
        covered = [x for x in eng.db.protective(sym) if x["kind"] == "sl"
                   and x["status"] in ("active", "unknown")
                   and not (x.get("note") or "").startswith(("extended-hours soft stop", "exit:"))]
        exits = [x for x in eng.db.protective(sym) if x["status"] in ("active", "unknown")
                 and (x.get("note") or "").startswith("exit:")]
        if covered or exits:
            return
        stop = float(t["stop_price"])
        try:
            price = await eng.broker.last_price(sym)
        except Exception:  # noqa: BLE001
            price = None
        if session == "CORE" and price and price <= stop:
            await pf.exit_position(sym, held, float(price),
                                   f"soft-stop sell unfilled at the open; price {price:.2f} <= stop {stop:.2f}")
            return
        coid = _coid()
        row = eng.db.add_protective(t["proposal_id"], sym, "sl", coid, held, stop, "unknown",
                                    "CORE stop restored after an unfilled soft-stop sell")
        try:
            await eng.broker.place_stop(sym, held, stop, coid, allow_when_killed=True)
        except BrokerError as e:
            if not e.transport:
                eng.db.set_protective(row, "closed", f"stop not placed: {e}")
            eng.db.log("error", f"{sym}: could not restore the CORE stop at {stop}: {e}. "
                                "The periodic check will retry.", alert=True)
            return
        eng.db.set_protective(row, "active", "CORE stop restored after an unfilled soft-stop sell")
        eng.db.log("warn", f"{sym}: the soft-stop sell ended unfilled; CORE stop restored for "
                           f"{held:g} at {stop:.2f}", alert=True)


class RiskLoopMixin:
    def _init_runtime(self) -> None:
        """Runtime helpers that need the engine's fields: called at the end of CoreMixin init."""
        self._atm_iv_day: dict[str, str] = {}
        self._atm_iv_attempt: dict[str, float] = {}
        self.option_exits = OptionExitManager(self)
        self.risk_loop = RiskLoop(self)
        self.workers = Workers(self)

    async def start_risk_loop(self) -> None:
        self.risk_loop.start()

    async def stop_risk_loop(self) -> None:
        await self.risk_loop.stop()
