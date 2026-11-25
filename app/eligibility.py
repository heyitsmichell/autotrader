"""Entry eligibility checks - earnigns blackout, gap filter, first-15-min, etc."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .broker import Broker, BrokerError, parse_snapshot
from .config import Settings
from .market import now_et, now_utc, market_is_open, is_premarket
from .signals import SignalService

log = logging.getLogger(__name__)
_yf = None


@dataclass
class EligibilityResult:
    ok: bool
    reason: str | None = None
    details: dict | None = None


class EligibilityChecker:
    """Check entry eligibility for a symbol."""

    def __init__(self, settings: Settings, broker: Broker, signals: SignalService, db=None):
        self.s = settings
        self.broker = broker
        self.signals = signals
        self.db = db

    async def check(self, symbol: str, side: str, last_price: float, signal: dict | None = None) -> EligibilityResult:
        """Run all eligibility checks for a BUY order.

        Returns EligibilityResult with ok=True if all checks pass.
        """
        if not self.s.eligibility_enabled:
            return EligibilityResult(ok=True)

        if side != "BUY":
            # SELL orders don't need entry eligibility checks
            return EligibilityResult(ok=True)

        # 1. Earnings blackout
        if self.s.eligibility_earnings_blackout_days > 0:
            result = await self._check_earnings(symbol)
            if not result.ok:
                return result

        # 1b. Hard negative news event (downgrade, offering, probe, ...). DB only: no network.
        if getattr(self.s, "event_veto_enabled", True):
            result = self._check_event_veto(symbol)
            if not result.ok:
                return result

        # 2. Gap filter
        if self.s.eligibility_max_gap_pct > 0:
            result = await self._check_gap(symbol, last_price)
            if not result.ok:
                return result

        # 3. First 15 minutes of session
        if self.s.eligibility_first_15_min:
            result = self._check_first_15_min()
            if not result.ok:
                return result

        # 4. One starter per symbol
        if self.s.eligibility_one_starter_per_symbol:
            result = await self._check_one_starter(symbol)
            if not result.ok:
                return result

        # 5. Minimum price
        if self.s.eligibility_min_price > 0:
            result = self._check_min_price(last_price)
            if not result.ok:
                return result

        # 6. Minimum volume
        if self.s.eligibility_min_volume > 0:
            result = await self._check_min_volume(symbol)
            if not result.ok:
                return result

        # 7. Minimum bars (signal quality)
        if self.s.eligibility_min_bars > 0:
            result = await self._check_min_bars(symbol, signal)
            if not result.ok:
                return result

        # 8. Signal freshness
        if self.s.eligibility_signal_freshness_min > 0:
            result = self._check_signal_freshness(signal)
            if not result.ok:
                return result

        return EligibilityResult(ok=True)

    # Entries are swing trades (strategy.md: exits after days, not minutes), so every BUY is
    # treated as held overnight. Kept as a method so an intraday-only mode can override it.
    def _held_overnight(self) -> bool:
        return True

    async def _check_earnings(self, symbol: str, blackout_days: int | None = None) -> EligibilityResult:
        """Earnings blackout. Fails CLOSED when the calendar cannot be read and the entry would
        be held overnight (ELIGIBILITY_EARNINGS_FAIL_CLOSED, default on). In BROKER_MODE=mock
        there is no real calendar and no real money, so a missing calendar passes there."""
        fail_closed = (getattr(self.s, "eligibility_earnings_fail_closed", True)
                       and self._held_overnight())
        try:
            dates = await self._get_earnings_dates(symbol)
        except Exception as e:  # noqa: BLE001 - treated the same as "unavailable"
            log.warning("Earnings calendar lookup failed for %s: %s", symbol, e)
            dates = None
        if dates is None:
            if fail_closed and self.s.broker_mode != "mock":
                return EligibilityResult(
                    ok=False,
                    reason=f"Earnings blackout: calendar unavailable for {symbol}; not entering a "
                           "position held overnight without it (fails closed)",
                    details={"earnings_calendar": "unavailable"})
            return EligibilityResult(ok=True, details={"earnings_calendar": "unavailable"})

        today = now_et().date()
        if blackout_days is None:
            blackout_days = self.s.eligibility_earnings_blackout_days
        for earn_date_str in dates:
            try:
                earn_date = datetime.fromisoformat(str(earn_date_str)[:10]).date()
            except ValueError:
                continue
            days_diff = (earn_date - today).days
            if -blackout_days <= days_diff <= blackout_days:
                return EligibilityResult(
                    ok=False,
                    reason=f"Earnings blackout: {symbol} has earnings on {earn_date} "
                           f"(within {blackout_days} days)",
                    details={"earnings_date": str(earn_date_str), "days_diff": days_diff})
        return EligibilityResult(ok=True)

    def _check_event_veto(self, symbol: str) -> EligibilityResult:
        """Block new BUYs for EVENT_VETO_HOURS after a relevant negative hard-event headline
        (analyst downgrade, guidance cut, offering/dilution, SEC/DOJ probe or lawsuit, FDA
        rejection, bankruptcy/going concern, deal-pending takeover target, delisting).
        Positive events never force a buy. A headline-store error lets the entry through: the
        veto is an extra filter, not a data dependency."""
        from .news_events import event_label, find_negative_event, fmt_age
        db = self.db or getattr(getattr(self.signals, "sentiment", None), "db", None)
        if db is None:
            return EligibilityResult(ok=True)
        hours = float(getattr(self.s, "event_veto_hours", 48.0))
        try:
            since = (now_utc() - timedelta(hours=hours)).isoformat(timespec="seconds")
            items = db.news(symbol, since=since, limit=200)
            hit = find_negative_event(symbol, items, now=now_utc(), hours=hours)
        except Exception as e:  # noqa: BLE001
            log.warning("Event veto lookup failed for %s: %s", symbol, e)
            return EligibilityResult(ok=True)
        if hit is None:
            return EligibilityResult(ok=True)
        return EligibilityResult(
            ok=False,
            reason=f"Event veto: {event_label(hit['event'])} — {hit['title']} "
                   f"({hit['source']}, {fmt_age(hit['age_hours'])})",
            details={"event": hit["event"], "headline": hit["title"], "source": hit["source"],
                     "age_hours": round(hit["age_hours"], 1), "news_id": hit["news_id"]})

    async def _get_earnings_dates(self, symbol: str) -> list[str] | None:
        """Upcoming earnings dates (YYYY-MM-DD), ``[]`` when none are scheduled, or ``None``
        when no source could be read. Order: injected provider, the broker's
        ``get_stock_earnings_calendar`` tool, then Yahoo Finance."""
        provider = getattr(self, "earnings_provider", None)
        if provider is not None:
            res = provider(symbol)
            if asyncio.iscoroutine(res):
                res = await res
            return None if res is None else [str(d) for d in res]

        names = {getattr(t, "name", None) for t in (getattr(self.broker, "tools", None) or [])}
        if "get_stock_earnings_calendar" in names:
            try:
                text = await self.broker.call("get_stock_earnings_calendar", symbol=symbol)
                if text is not None and not str(text).startswith(("Error", "Validation error")):
                    return re.findall(r"\d{4}-\d{2}-\d{2}", str(text))
            except Exception as e:  # noqa: BLE001 - try the next source
                log.warning("Broker earnings calendar failed for %s: %s", symbol, e)

        try:
            global _yf
            if _yf is None:
                from .yfinance_service import YahooFinanceService
                _yf = YahooFinanceService(self.s)   # shared: keeps its cache and circuit breaker
            # Real calendar + history, cached per symbol (EARNINGS_CACHE_HOURS). None = unreadable.
            return await _yf.get_earnings_dates(symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("Yahoo earnings lookup failed for %s: %s", symbol, e)
            return None

    async def _check_gap(self, symbol: str, last_price: float) -> EligibilityResult:
        """Check if the overnight/weekend gap exceeds the threshold."""
        try:
            # Get previous close from the broker snapshot (uses batched fetch with TTL cache)
            snap = await self.broker.get_snapshot(symbol)
            if not snap:
                log.warning(f"Gap check failed for {symbol}: snapshot data unavailable")
                return EligibilityResult(ok=False, reason="Gap filter: data unavailable")

            # Extract previous close from the quote
            prev_close = snap.get("prev_close")
            if not prev_close or prev_close <= 0:
                log.warning(f"Gap check failed for {symbol}: prev_close missing or invalid ({prev_close})")
                return EligibilityResult(ok=False, reason="Gap filter: data unavailable")

            prev_close = float(prev_close)
            gap_pct = abs(last_price - prev_close) / prev_close * 100

            if gap_pct > self.s.eligibility_max_gap_pct:
                return EligibilityResult(
                    ok=False,
                    reason=f"Gap filter: {symbol} gapped {gap_pct:.2f}% (prev close ${prev_close:.2f}, last ${last_price:.2f}, max {self.s.eligibility_max_gap_pct}%)",
                    details={"gap_pct": gap_pct, "prev_close": prev_close, "last_price": last_price}
                )

            return EligibilityResult(ok=True)
        except Exception as e:
            log.warning(f"Gap check failed for {symbol}: {e}")
            return EligibilityResult(ok=False, reason="Gap filter: data unavailable")

    def _check_first_15_min(self) -> EligibilityResult:
        """Block trades in the first 15 minutes of the REGULAR trading session.

        Does not apply during premarket (4:00-9:30 AM ET).
        """
        if not market_is_open():
            # If regular session is closed, this check doesn't apply
            # (premarket is handled separately - no 15-min rule there)
            return EligibilityResult(ok=True)

        # Skip if we're in premarket
        if is_premarket():
            return EligibilityResult(ok=True)

        now = now_et()
        # Market opens at 9:30 AM ET
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        fifteen_min_later = market_open + timedelta(minutes=15)

        if now < fifteen_min_later:
            remaining = fifteen_min_later - now
            return EligibilityResult(
                ok=False,
                reason=f"First 15-min rule: {remaining.seconds // 60}m {remaining.seconds % 60}s until 15-min mark",
                details={"seconds_remaining": remaining.total_seconds()}
            )

        return EligibilityResult(ok=True)

    async def _check_one_starter(self, symbol: str) -> EligibilityResult:
        """Ensure only one active position/starter per symbol."""
        try:
            # Check current positions
            positions = await self.broker.positions()
            if positions.get(symbol, 0) > 0:
                return EligibilityResult(
                    ok=False,
                    reason=f"One starter per symbol: already holding {positions[symbol]:g} shares of {symbol}",
                    details={"current_position": positions[symbol]}
                )

            # Check working orders at broker (these are live at the exchange)
            open_text = await self.broker.open_orders_text()
            if open_text and symbol in open_text:
                from .portfolio import parse_orders
                orders = parse_orders(open_text)
                for o in orders:
                    if o.get("symbol") == symbol and o.get("side") == "BUY" and o.get("status") in ("WORKING", "PENDING", "SUBMITTED"):
                        return EligibilityResult(
                            ok=False,
                            reason=f"One starter per symbol: working BUY order for {symbol} at broker",
                            details={"order_id": o.get('client_order_id')}
                        )

            # Check pending proposals that will be auto-executed (trading_mode == "auto")
            # In approve mode, user manually approves so multiple pending is OK
            if self.db and self.s.trading_mode == "auto":
                pending = self.db.proposals(symbol=symbol)
                for p in pending:
                    if p.get("side") == "BUY" and p.get("status") in ("pending", "placed", "unknown", "submitting"):
                        return EligibilityResult(
                            ok=False,
                            reason=f"One starter per symbol: {p.get('status')} BUY proposal for {symbol} (id={p.get('id')}) will be auto-executed",
                            details={"pending_proposal_id": p.get('id'), "status": p.get('status')}
                        )

            return EligibilityResult(ok=True)
        except Exception as e:
            log.warning(f"One starter check failed for {symbol}: {e}")
            return EligibilityResult(ok=True)

    def _check_min_price(self, last_price: float) -> EligibilityResult:
        """Check minimum share price."""
        if last_price < self.s.eligibility_min_price:
            return EligibilityResult(
                ok=False,
                reason=f"Min price: ${last_price:.2f} < ${self.s.eligibility_min_price:.2f}",
                details={"price": last_price, "min_price": self.s.eligibility_min_price}
            )
        return EligibilityResult(ok=True)

    MIN_VOLUME_SESSIONS = 20
    MIN_VOLUME_MIN_BARS = 5

    async def _avg_daily_dollar_volume(self, symbol: str) -> tuple[float, int] | None:
        """(average close*volume, sessions used) over the last completed daily bars, or None.

        Today's bar is dropped: it is a partial session (tiny premarket) and would reject
        every liquid name. Bars come from the signal engine's cache, else one broker fetch.
        """
        from .broker.parsers import bar_day
        bars = None
        cached = getattr(self.signals, "cached_bars", None)
        if cached:
            bars = cached(symbol, max_age_seconds=86400)
        if not bars:
            try:
                bars = await self.broker.daily_bars(symbol, self.MIN_VOLUME_SESSIONS + 5)
            except Exception as e:  # noqa: BLE001 - fall back to the snapshot
                log.debug("daily bars for %s unavailable for the volume check: %s", symbol, e)
                return None
        today = now_et().date()
        done = [b for b in bars if bar_day(b["time"]) < today and b.get("volume") and b.get("close")]
        done = done[-self.MIN_VOLUME_SESSIONS:]
        if len(done) < self.MIN_VOLUME_MIN_BARS:
            return None
        return sum(b["close"] * b["volume"] for b in done) / len(done), len(done)

    async def _check_min_volume(self, symbol: str) -> EligibilityResult:
        """Check minimum average daily dollar volume (20 completed sessions)."""
        try:
            avg = await self._avg_daily_dollar_volume(symbol)
            if avg is not None:
                daily_volume_usd, n = avg
                label = f"avg daily $ volume ({self.MIN_VOLUME_SESSIONS}d)"
            else:
                # No usable history: fall back to the live snapshot.
                snap = await self.broker.get_snapshot(symbol)
                if not snap:
                    log.warning(f"Min volume check failed for {symbol}: snapshot data unavailable")
                    return EligibilityResult(ok=False, reason="Min volume: data unavailable")
                volume = snap.get("prev_volume", snap.get("volume"))
                last_price = snap.get("price")
                if volume is None or last_price is None or volume < 0 or last_price <= 0:
                    log.warning(f"Min volume check failed for {symbol}: invalid volume ({volume}) or price ({last_price})")
                    return EligibilityResult(ok=False, reason="Min volume: data unavailable")
                if "prev_volume" not in snap and not market_is_open():
                    # Today's volume is a partial session here, so it cannot be judged.
                    log.warning("Min volume for %s: no daily history and the session is partial; "
                                "not failing on partial-session volume", symbol)
                    return EligibilityResult(ok=True)
                daily_volume_usd = float(volume) * float(last_price)
                label = "daily $ volume (snapshot)"
            if daily_volume_usd < self.s.eligibility_min_volume:
                return EligibilityResult(
                    ok=False,
                    reason=f"Min volume: {symbol} {label} ${daily_volume_usd:,.0f} < ${self.s.eligibility_min_volume:,.0f}",
                    details={"daily_volume_usd": daily_volume_usd, "min_volume_usd": self.s.eligibility_min_volume}
                )
            return EligibilityResult(ok=True)
        except Exception as e:
            log.warning(f"Min volume check failed for {symbol}: {e}")
            return EligibilityResult(ok=False, reason="Min volume: data unavailable")

    async def _check_min_bars(self, symbol: str, signal: dict | None) -> EligibilityResult:
        """Check if signal has sufficient bars for quality."""
        if signal and signal.get("features"):
            # Check the number of bars used for signal calculation
            # The signal should have a bar count or we can infer from features
            pass
        # For now, this is a placeholder - would need signal metadata
        return EligibilityResult(ok=True)

    def _check_signal_freshness(self, signal: dict | None) -> EligibilityResult:
        """Check if signal is fresh enough."""
        if not signal:
            return EligibilityResult(
                ok=False,
                reason=f"Signal freshness: no signal available",
                details={"max_age_min": self.s.eligibility_signal_freshness_min}
            )

        signal_time = signal.get("time") or signal.get("timestamp")
        if signal_time:
            try:
                if isinstance(signal_time, str):
                    signal_dt = datetime.fromisoformat(signal_time.replace("Z", "+00:00"))
                elif isinstance(signal_time, (int, float)):
                    signal_dt = datetime.fromtimestamp(signal_time, tz=timezone.utc)
                else:
                    return EligibilityResult(ok=True)

                age = now_utc() - signal_dt
                if age > timedelta(minutes=self.s.eligibility_signal_freshness_min):
                    return EligibilityResult(
                        ok=False,
                        reason=f"Signal stale: {age.total_seconds()/60:.0f} minutes old (max {self.s.eligibility_signal_freshness_min})",
                        details={"age_minutes": age.total_seconds()/60, "max_age_min": self.s.eligibility_signal_freshness_min}
                    )
            except Exception:
                pass

        return EligibilityResult(ok=True)


async def check_eligibility(
    settings: Settings,
    broker: Broker,
    signals: SignalService,
    symbol: str,
    side: str,
    last_price: float,
    signal: dict | None = None,
    db=None
) -> EligibilityResult:
    """Convenience function to check eligibility."""
    checker = EligibilityChecker(settings, broker, signals, db)
    return await checker.check(symbol, side, last_price, signal)