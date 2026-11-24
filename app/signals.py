"""Signal snapshots: TA + sentiment + ML for every watchlist symbol.

Computed at the start of every AI run, on a free background timer during market
hours, and on demand. Each snapshot is stored, which gives the dashboard its
"why" data and gives the learning loop many more labelled examples than trades.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from . import consensus, indicators, ta
from .broker import Broker, BrokerError, bar_day
from .config import Settings
from .db import DB, decode
from .market import market_is_open, market_is_open_extended, now_utc, order_session
from .ml import MLFilter
from .sentiment import SentimentEngine

log = logging.getLogger(__name__)
BARS = 260


class SignalService:
    def __init__(self, settings: Settings, broker: Broker, db: DB,
                 sentiment: SentimentEngine, ml: MLFilter, research=None) -> None:
        self.s, self.broker, self.db = settings, broker, db
        self.sentiment, self.ml, self.research = sentiment, ml, research
        self.refreshing = False
        self.error: str | None = None
        self.last_refresh: datetime | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._bars_cache: dict[str, tuple[datetime, list[dict]]] = {}
        self._bars_full_at: dict[str, datetime] = {}     # last full (not incremental) download
        self._invalid: dict[str, float] = {}              # symbol -> time.time() until skipped

    # Daily bars barely change: reuse them for BARS_TTL_SECONDS, then fetch only the last few
    # bars and merge, with a full download every BARS_FULL_SECONDS.
    REFRESH_DEADLINE_SECONDS = 100.0     # below the engine's 120 s cold-refresh wait_for
    BARS_TTL_SECONDS = 900.0
    BARS_FULL_SECONDS = 6 * 3600.0
    BARS_INCREMENT = 5
    INVALID_SYMBOL_TTL_SECONDS = 24 * 3600.0

    def _is_invalid(self, sym: str) -> bool:
        until = self._invalid.get(sym)
        if until is None:
            return False
        if time.time() >= until:
            del self._invalid[sym]
            return False
        return True

    def _mark_invalid(self, sym: str) -> None:
        if sym in self._invalid:
            return
        self._invalid[sym] = time.time() + self.INVALID_SYMBOL_TTL_SECONDS
        self.db.log("warn", f"{sym} is not tradable at the broker (INVALID_SYMBOL); skipped for "
                            f"{self.INVALID_SYMBOL_TTL_SECONDS / 3600:.0f} h (or remove it from WATCHLIST)")

    @staticmethod
    def _day_key(b: dict):
        t = b["time"]
        try:
            return bar_day(t)
        except Exception:  # noqa: BLE001 - unparseable stamp: fall back to the raw value
            return t

    def _bars_ttl(self) -> float:
        """Cached daily bars live BARS_TTL_SECONDS, but while a session is open the last
        (partial) bar drives intraday TA, so cap the TTL at the refresh interval."""
        try:
            if market_is_open_extended():
                mins = getattr(self.s, "signal_refresh_minutes", 0) or 0
                if mins > 0:
                    return min(self.BARS_TTL_SECONDS, mins * 60.0)
        except Exception:  # noqa: BLE001
            pass
        return self.BARS_TTL_SECONDS

    async def _load_bars(self, sym: str) -> list[dict]:
        now = now_utc()
        hit = self._bars_cache.get(sym)
        if hit and len(hit[1]) >= 60:
            age = (now - hit[0]).total_seconds()
            if age < self._bars_ttl():
                return hit[1]
            full_at = self._bars_full_at.get(sym)
            if full_at and (now - full_at).total_seconds() < self.BARS_FULL_SECONDS:
                recent = await self.broker.daily_bars(sym, self.BARS_INCREMENT)
                # One bar per trading day: today's partial bar may return with another stamp.
                merged = {self._day_key(b): b for b in hit[1]}
                merged.update({self._day_key(b): b for b in recent})
                out = [merged[k] for k in sorted(merged)][-BARS:]
                self._bars_cache[sym] = (now, out)
                return out
        out = await self.broker.daily_bars(sym, BARS)
        self._bars_cache[sym] = (now, out)
        self._bars_full_at[sym] = now
        return out

    async def refresh(self, run_id: int | None = None, symbols: list[str] | None = None,
                      with_llm: bool | None = None) -> list[dict]:
        symbols = symbols or self.s.watchlist
        # The deadline starts BEFORE waiting for the lock: if a background refresh already holds
        # it, the cycle's wait_for(120) must not burn its whole budget queued. We wait for the
        # lock at most the deadline (the background run then leaves fresh bars in the cache, so
        # our own pass is quick); on timeout we raise and the cycle uses the stored signals.
        started = time.monotonic()
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self.REFRESH_DEADLINE_SECONDS)
        except asyncio.TimeoutError:
            raise RuntimeError("signal refresh busy: another refresh held the lock past the "
                               "deadline") from None
        remaining = max(1.0, self.REFRESH_DEADLINE_SECONDS - (time.monotonic() - started))
        try:
            self.refreshing = True
            try:
                bars: dict[str, list[dict]] = {}
                skipped: list[str] = []
                symbols = [x for x in symbols if not self._is_invalid(x)]

                # Bounded concurrency: with a wide universe (WATCHLIST_UNIVERSES) an unbounded
                # gather fires one request per symbol at once and trips Webull's rate limit.
                sem = asyncio.Semaphore(max(1, getattr(self.s, "bars_concurrency", 6)))

                async def load(sym: str) -> None:
                    async with sem:
                        try:
                            bars[sym] = await self._load_bars(sym)
                        except BrokerError as e:
                            if "INVALID_SYMBOL" in str(e):
                                self._mark_invalid(sym)
                            else:
                                log.warning("bars for %s failed: %s", sym, e)
                                skipped.append(sym)

                async def news() -> None:
                    # News runs alongside the bar downloads and can never block or fail them.
                    try:
                        await self.sentiment.refresh_background(symbols, with_llm)
                    except Exception as e:  # noqa: BLE001
                        log.warning("news refresh failed, signals continue without it: %s", e)

                # Own deadline, shorter than the caller's wait_for: a cancelled refresh would
                # throw away every bar already downloaded; this keeps the partial result.
                tasks = {s: asyncio.create_task(load(s)) for s in symbols}
                news_task = asyncio.create_task(news())
                try:
                    _, pending = await asyncio.wait([news_task, *tasks.values()],
                                                    timeout=remaining)
                except BaseException:
                    # outer cancellation (cycle's wait_for): do not leak the workers
                    for t in (news_task, *tasks.values()):
                        t.cancel()
                    await asyncio.gather(news_task, *tasks.values(), return_exceptions=True)
                    raise
                for t in pending:
                    t.cancel()
                for t in _:
                    if not t.cancelled() and t.exception():
                        log.warning("bars task failed: %r", t.exception())
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                    skipped += [s for s, t in tasks.items() if t in pending and s not in bars]
                    log.warning("signal refresh hit its %.0fs deadline; %d symbol(s) skipped: %s",
                                self.REFRESH_DEADLINE_SECONDS, len(skipped), ", ".join(skipped))
                out = []
                for sym in symbols:
                    if len(bars.get(sym, [])) < 60:
                        continue
                    out.append(self._store(run_id, sym, bars[sym]))
                self.error = None if out else "No symbol had enough daily bars"
                self.last_refresh = now_utc()
                if run_id:
                    self.db.set_run_signals(run_id, len(out))
                return out
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                raise
            finally:
                self.refreshing = False
        finally:
            self._lock.release()

    def _store(self, run_id: int | None, sym: str, bars: list[dict]) -> dict:
        f = indicators.features(bars)
        t = ta.analyze(f, self.s)
        sent = self.sentiment.summary(sym)

        # Compute relative strength vs benchmark if enabled
        relative_strength = None
        market_trend = "range"
        market_strength = "n/a"
        market_adx = None
        # Compute market regime data if either relative strength or market regime filter is enabled
        need_market_regime = (getattr(self.s, "relative_strength_enabled", False) or
                               getattr(self.s, "market_regime_filter_enabled", False))
        if need_market_regime:
            benchmark_symbol = getattr(self.s, "market_regime_symbol", "SPY")
            # Get benchmark bars from cache or fetch
            bench_bars = self._bars_cache.get(benchmark_symbol)
            if bench_bars and len(bench_bars[1]) >= len(bars):
                bench_bars_list = bench_bars[1]
                # Calculate relative strength over the lookback period
                lookback = getattr(self.s, "relative_strength_lookback", 20)
                if len(bars) >= lookback and len(bench_bars_list) >= lookback:
                    # Momentum = (close / close_lookback_ago) - 1
                    sym_momentum = (bars[-1]["close"] / bars[-lookback]["close"]) - 1
                    bench_momentum = (bench_bars_list[-1]["close"] / bench_bars_list[-lookback]["close"]) - 1
                    relative_strength = (sym_momentum - bench_momentum) * 100  # in percent

                # Also compute market regime from benchmark
                bench_features = indicators.features(bench_bars_list)
                bench_ta = ta.analyze(bench_features[-1], self.s)
                market_trend = bench_ta.get("trend", "range")
                market_strength = bench_ta.get("strength", "n/a")
                market_adx = bench_features[-1].get("adx14")

        row = {
            "symbol": sym, "close": t["close"], "atr": t["atr"], "trend": t["trend"],
            "strength": t["strength"], "vol": t["vol"], "ta_score": t["ta_score"],
            "ta_components": t["ta_components"], "features": f,
            "sent_score": sent["score"], "sent_webull": sent["webull"]["score"],
            "sent_llm": sent["llm"]["score"], "sent_n": sent["n"],
            "sent_pos": sent["webull"]["pos"], "sent_neg": sent["webull"]["neg"],
            "sent_neu": sent["webull"]["neu"],
            "fear_greed": (sent.get("fear_greed") or {}).get("index"),
            "ml_p": self.ml.predict(f), "ml_model_id": self.ml.model_id,
            # New fields for strategy improvements
            "relative_strength": relative_strength,
            "market_trend": market_trend,
            "market_strength": market_strength,
            "market_adx": market_adx,
        }
        gate = consensus.evaluate({"side": "BUY", "limit_price": t["close"]}, row, self.s)
        row.update(consensus="long" if gate["ok"] else "blocked",
                   consensus_checks=gate["checks"], sl=gate["sl"], tp=gate["tp"])
        sid = self.db.add_signal(run_id, row)
        return decode(self.db.get_signal(sid))

    def cached_bars(self, symbol: str, max_age_seconds: float = 1800) -> list[dict] | None:
        """Daily bars from the last refresh, or None if missing/older than max_age_seconds."""
        hit = self._bars_cache.get(symbol)
        if not hit or (now_utc() - hit[0]).total_seconds() > max_age_seconds:
            return None
        return hit[1]

    def latest(self, symbol: str) -> dict | None:
        rows = self.db.signals(symbol, 1)
        return decode(rows[0]) if rows else None

    async def ensure(self, symbol: str, max_age_minutes: int = 20) -> dict | None:
        """Latest signal for a symbol, refreshed first if it is missing or stale."""
        sig = self.latest(symbol)
        fresh = sig and now_utc() - datetime.fromisoformat(sig["created_at"]) < timedelta(
            minutes=max_age_minutes)
        if fresh:
            return sig
        try:
            await self.refresh(symbols=[symbol], with_llm=False)
        except Exception as e:  # noqa: BLE001
            log.warning("signal refresh for %s failed: %s", symbol, e)
        return self.latest(symbol)

    def describe(self, symbol: str | None = None) -> str:
        """Plain-text view for the analyst (get_signals)."""
        rows = [decode(r) for r in self.db.latest_signals()]
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol.strip().upper()]
        if not rows:
            return "No signals yet."
        lines = []
        for r in rows:
            gate = "; ".join(f"{c['name']} {'ok' if c['ok'] else 'FAIL'}"
                             for c in r.get("consensus_checks") or [])
            sent = "n/a" if r["sent_score"] is None else f"{r['sent_score']:+.2f}"
            ml = "no model" if r["ml_p"] is None else f"{r['ml_p']:.0%}"
            lines.append(
                f"{r['symbol']} @ {r['created_at']}: close {r['close']}, trend {r['trend']} "
                f"({r['strength']}, vol {r['vol']}), TA score {r['ta_score']:+.2f}, "
                f"news {sent} from {r['sent_n']} headline(s), ML {ml}. "
                f"Consensus for a BUY now: {r['consensus'].upper()} ({gate}). "
                f"Code-set levels for a BUY at the close: stop {r['sl']}, target {r['tp']}.")
        return "\n".join(lines)

    # -- background refresh (free: no LLM calls unless SENTIMENT_LLM is on) -------------
    def start(self) -> None:
        if self.s.signal_refresh_minutes > 0:
            self._task = asyncio.create_task(self._loop(), name="signals")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(8)
        first = True
        while True:
            try:
                if self.s.market_hours_only:
                    session_ok = market_is_open()
                elif self.s.overnight_trading:
                    session_ok = order_session() is not None
                else:
                    session_ok = market_is_open_extended()
                if self.broker.connected and (first or session_ok) and not self.refreshing:
                    watchlist = self.research.effective_watchlist() if self.research else self.s.watchlist
                    await self.refresh(symbols=watchlist, with_llm=False)
                    first = False
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("signal refresh")
            await asyncio.sleep(self.s.signal_refresh_minutes * 60)
