"""Fast loop: re-score "armed" symbols on the live price between the slow analysis cycles.

The slow cycle (signals refresh + run_cycle) scores daily bars every few minutes. A pullback can
reach the entry setup in between, and a limit order placed minutes later may never fill. This
loop closes that gap without changing the strategy or the risk rules:

1. ARM: from the latest stored signals, pick symbols that are close to passing the gate (TA score
   within FAST_LOOP_ARM_MARGIN of CONSENSUS_TA_MIN, sentiment and ML already fine, not held, no
   pending BUY, not on cooldown), plus held positions watched for live ML exits.
2. WATCH: read their live price from the existing MarketFeed (push stream, or one batched
   snapshot poll). No per-symbol quote request in streaming mode.
3. RE-SCORE: put the live price into the last daily bar, recompute the TA features and run the
   SAME consensus gate (consensus.evaluate) the slow cycle uses (or check live ML exit floor).
4. TRIGGER: if it passes, submit a BUY or SELL through Engine._on_proposal, i.e. the normal
   eligibility, code sizing, risk checks, kill switch and approve/auto mode.

It runs in the same sessions as the scheduler (regular hours with MARKET_HOURS_ONLY=true, the
default; premarket/after-hours, and overnight with OVERNIGHT_TRADING=true, otherwise).
It is off by default (FAST_LOOP_ENABLED=false).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, time as dtime, timezone

from . import consensus, indicators, ta
from .broker import BrokerError, bar_day
from .config import Settings
from .risk import working_buy_symbols
from .market import market_is_open, market_is_open_extended, now_et, order_session

log = logging.getLogger(__name__)

PRICE_MAX_AGE_SECONDS = 60      # a pushed/polled tick older than this is not trusted
BARS_MAX_AGE_SECONDS = 1800     # daily bars from the last refresh must be this fresh
# These two checks depend on the live price, so they are re-evaluated rather than pre-filtered.
LIVE_CHECKS = {"TA direction", "TA regime"}


def with_live_price(bars: list[dict], price: float, today: date) -> list[dict]:
    """Copy of the daily bars whose last bar reflects `price`.

    If the last bar is today's, its close moves to `price` and high/low widen to include it.
    If it is an earlier day, a bar for today is appended (open = previous close, volume copied
    from the previous bar so volume-based features stay neutral instead of dropping to zero).
    """
    if not bars or not price or price <= 0:
        return [dict(b) for b in bars]
    out = [dict(b) for b in bars]
    last = out[-1]
    t = last.get("time")
    last_day = bar_day(t) if isinstance(t, datetime) else None
    if last_day is not None and last_day > today:
        return out
    if last_day == today:
        last["close"] = price
        last["high"] = max(last.get("high", price), price)
        last["low"] = min(last.get("low", price), price)
        return out
    prev_close = last["close"]
    new = dict(last)
    new.update(time=datetime.combine(today, dtime(0, 0), tzinfo=timezone.utc), open=prev_close,
               high=max(prev_close, price), low=min(prev_close, price), close=price,
               synthetic=True)
    out.append(new)
    return out


def select_armed(rows: list[dict], held: set[str], pending_buys: set[str], cooldown: set[str],
                 s: Settings) -> list[str]:
    """Symbols worth watching on the live price, closest to the entry setup first."""
    ranked: list[tuple[float, str]] = []
    for r in rows:
        sym = r.get("symbol")
        score = r.get("ta_score")
        if not sym or score is None:
            continue
        if sym in held or sym in pending_buys or sym in cooldown:
            continue
        ml_first = bool(getattr(s, "ml_enabled", False) and getattr(s, "ml_first", False)
                        and r.get("ml_p") is not None)
        if not ml_first and (r.get("trend") or "range") == "down":
            continue
        ta_floor = min(getattr(s, "ml_first_ta_floor", -1.0), s.consensus_ta_min) if ml_first else s.consensus_ta_min
        gap = ta_floor - score
        if gap > s.fast_loop_arm_margin:
            continue
        # sentiment / ML come from the slow cycle: if they already block, a price move won't fix it
        if any(not c.get("ok", True) for c in (r.get("consensus_checks") or [])
               if c.get("name") not in LIVE_CHECKS):
            continue
        ranked.append((gap, sym))
    ranked.sort()
    return [sym for _, sym in ranked[:s.fast_loop_max_armed]]


class FastLoop:
    def __init__(self, settings: Settings, broker, db, engine, signals, feed=None) -> None:
        self.s, self.broker, self.db = settings, broker, db
        self.engine, self.signals, self.feed = engine, signals, feed
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None
        self._subscribed: set[str] = set()
        self._cooldown_until: dict[str, float] = {}
        self._sell_cooldown_until: dict[str, float] = {}
        self.armed: list[str] = []
        self.ticks = 0
        self.triggers = 0
        self.last_tick_at: str | None = None
        self.last_error: str | None = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self.s.fast_loop_enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="fast-loop")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._sync_subscriptions([])

    async def _loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - never let the loop die
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("fast loop tick failed")
            await asyncio.sleep(self.s.fast_loop_seconds)

    def status(self) -> dict:
        return {"enabled": self.s.fast_loop_enabled, "running": self._task is not None,
                "armed": list(self.armed), "ticks": self.ticks, "triggers": self.triggers,
                "last_tick_at": self.last_tick_at, "last_error": self.last_error,
                "seconds": self.s.fast_loop_seconds}

    # -- one pass --------------------------------------------------------------
    def _session_open(self) -> bool:
        """Same session rule as the scheduler and the signal refresh: regular hours only when
        MARKET_HOURS_ONLY=true (the default), otherwise premarket/after-hours too, and the
        overnight session when OVERNIGHT_TRADING=true."""
        if getattr(self.s, "market_hours_only", True):
            return market_is_open()
        if getattr(self.s, "overnight_trading", False):
            return order_session() is not None
        return market_is_open_extended()

    async def tick(self) -> list[dict]:
        """One check of all armed symbols. Returns the triggers it fired (for tests and logs)."""
        self.ticks += 1
        # the engine's settings, so lessons applied at runtime reach the fast loop's gate too
        self.s = getattr(self.engine, "s", None) or self.s
        self.last_tick_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if self.engine.kill_switch or not self.broker.connected or not self._session_open():
            self._set_armed([])
            return []
        if self.engine._run_lock.locked():
            return []  # the slow cycle is working; don't race it

        watchlist = (self.engine.research.effective_watchlist()
                     if getattr(self.engine, "research", None) else self.s.watchlist)
        rows = [r for r in (self._signal(sym) for sym in watchlist) if r]
        try:
            positions = await self.broker.positions()
        except BrokerError as e:
            self.last_error = f"positions: {e}"
            return []
        held = {sym for sym, qty in positions.items() if qty > 0}
        by_symbol = {r["symbol"]: r for r in rows}

        pending_all = self.db.pending_proposals()
        pending_buys = {p["symbol"] for p in pending_all if p["side"] == "BUY"}
        # a BUY already sent to the broker but not filled is neither held nor pending: without
        # this the symbol is re-armed every cooldown and bought again and again
        pending_buys |= working_buy_symbols(self.db)
        pending_sells = {p["symbol"] for p in pending_all if p["side"] == "SELL"}
        now = time.time()
        cooling = {sym for sym, until in self._cooldown_until.items() if until > now}
        sell_cooling = {sym for sym, until in self._sell_cooldown_until.items() if until > now}

        armed = select_armed(rows, held, pending_buys, cooling, self.s)
        exit_watch = (
            [sym for sym in held if sym in by_symbol and sym not in pending_sells and sym not in sell_cooling]
            if getattr(self.s, "ml_enabled", False) and getattr(self.s, "exit_ml_drop_enabled", True)
            else []
        )
        self._set_armed(list(dict.fromkeys([*armed, *exit_watch])))

        fired: list[dict] = []
        watch_all = list(dict.fromkeys([*exit_watch, *armed]))
        # Warm the broker's snapshot cache with ONE batched request, so the per-symbol
        # _price() calls below hit the cache instead of making one request each (HTTP 429).
        prefetch = getattr(self.broker, "_fetch_snapshots", None)
        if watch_all and prefetch is not None:
            try:
                await prefetch(watch_all)
            except BrokerError:
                pass

        for sym in exit_watch:
            if self._blocked():
                break
            price = await self._price(sym)
            if price is None:
                continue
            hit = await self._check_exit(sym, price, positions[sym], by_symbol[sym])
            if hit:
                fired.append(hit)

        for sym in armed:
            if self._blocked():
                break  # the kill switch went on (or the broker dropped) while we were awaiting
            price = await self._price(sym)
            if price is None:
                continue
            hit = await self._check(sym, price, by_symbol[sym])
            if hit:
                fired.append(hit)
        return fired

    def _signal(self, sym: str) -> dict | None:
        """The cold-path signal for ``sym`` (M4). With the engine's ``SignalStore`` present this
        is TTL-checked: a stale or missing entry gives None, so a stalled worker means no fast-loop
        entries (fail closed). Engines without a store fall back to the latest stored row."""
        store = getattr(self.engine, "signal_store", None)
        if store is not None:
            return store.get(sym)
        return self.signals.latest(sym)

    def _blocked(self) -> bool:
        return bool(self.engine.kill_switch) or not self.broker.connected

    def _set_armed(self, armed: list[str]) -> None:
        self.armed = armed
        self._sync_subscriptions(armed)

    def _sync_subscriptions(self, armed: list[str]) -> None:
        """Keep the market feed polling/streaming exactly the armed symbols."""
        if self.feed is None or getattr(self.feed, "mode", "off") == "off":
            return
        want = set(armed)
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=1)  # never drained on purpose; the feed keeps `last`
        for sym in want - self._subscribed:
            self.feed.subscribe(sym, self._queue)
        for sym in self._subscribed - want:
            self.feed.unsubscribe(sym, self._queue)
        self._subscribed = want

    async def _price(self, sym: str) -> float | None:
        if self.feed is not None:
            tick = self.feed.last.get(sym)
            if tick and time.time() - tick.get("t", 0) <= PRICE_MAX_AGE_SECONDS and tick.get("p", 0) > 0:
                return float(tick["p"])
        try:
            px = await self.broker.last_price(sym)
        except BrokerError:
            return None
        return float(px) if px and px > 0 else None

    async def _check(self, sym: str, price: float, row: dict) -> dict | None:
        bars = self.signals.cached_bars(sym, BARS_MAX_AGE_SECONDS)
        if not bars:
            return None  # the slow cycle will refresh them
        adjusted = with_live_price(bars, price, now_et().date())
        f = indicators.features(adjusted)
        if not f:
            return None
        t = ta.analyze(f, self.s)
        sig = {**row, "close": t["close"], "atr": t["atr"], "trend": t["trend"],
               "strength": t["strength"], "vol": t["vol"], "ta_score": t["ta_score"],
               "ta_components": t["ta_components"], "features": f,
               "ml_p": self.signals.ml.predict(f), "live": True, "live_price": price}
        p = {"symbol": sym, "side": "BUY", "quantity": 1, "limit_price": round(price, 2),
             "confidence": 0.7,
             "rationale": (f"Fast loop: live price {price:.2f} brought {sym} into the setup "
                           f"(TA {t['ta_score']:+.2f}, trend {t['trend']}); same gate as the slow cycle")}
        gate = consensus.evaluate(p, sig, self.s)
        if not gate["ok"]:
            return None
        # TOCTOU: the kill check at the top of tick() is several awaits old by now. The engine
        # re-checks under its proposal lock too; this keeps a killed loop from even opening a run.
        if self._blocked():
            return None

        self._cooldown_until[sym] = time.time() + self.s.fast_loop_cooldown_minutes * 60
        run_id = self.db.create_run("fast_loop", "deterministic")
        result = await self.engine._on_proposal(run_id, p, sig_override=sig)
        self.triggers += 1
        await self.engine._auto_execute_run(run_id)   # no-op unless TRADING_MODE=auto
        self.db.finish_run(run_id, "ok", summary=f"fast loop {sym} @ {price:.2f}: {result[:200]}")
        self.db.log("info", f"Fast loop {sym} @ {price:.2f}: {result[:200]}")
        return {"symbol": sym, "price": price, "run_id": run_id, "result": result}

    async def _check_exit(self, sym: str, price: float, qty: float, row: dict) -> dict | None:
        bars = self.signals.cached_bars(sym, BARS_MAX_AGE_SECONDS)
        if not bars:
            return None
        adjusted = with_live_price(bars, price, now_et().date())
        f = indicators.features(adjusted)
        if not f:
            return None
        ml_p = self.signals.ml.predict(f)
        if ml_p is None:
            return None
        floor = self.s.ml_min_p - getattr(self.s, "exit_ml_drop_margin", 0.0)
        if ml_p >= floor or self._blocked():
            return None

        t = ta.analyze(f, self.s)
        sig = {**row, "close": t["close"], "atr": t["atr"], "trend": t["trend"],
               "strength": t["strength"], "vol": t["vol"], "ta_score": t["ta_score"],
               "ta_components": t["ta_components"], "features": f,
               "ml_p": ml_p, "live": True, "live_price": price}
        p = {"symbol": sym, "side": "SELL", "quantity": qty, "limit_price": round(price, 2),
             "confidence": 0.8,
             "rationale": (f"Fast loop exit: live price {price:.2f} dropped ML probability to "
                           f"{ml_p:.0%} (below {floor:.0%})")}
        self._sell_cooldown_until[sym] = time.time() + self.s.fast_loop_cooldown_minutes * 60
        run_id = self.db.create_run("fast_loop", "deterministic")
        result = await self.engine._on_proposal(run_id, p, sig_override=sig)
        self.triggers += 1
        await self.engine._auto_execute_run(run_id)   # no-op unless TRADING_MODE=auto
        self.db.finish_run(run_id, "ok", summary=f"fast loop SELL {sym} @ {price:.2f}: {result[:200]}")
        self.db.log("info", f"Fast loop SELL {sym} @ {price:.2f}: {result[:200]}")
        return {"symbol": sym, "side": "SELL", "price": price, "run_id": run_id, "result": result}