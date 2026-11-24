"""Yahoo Finance integration service for fundamentals, corporate calendar, and market context."""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("trader.yfinance")


def _iso_date(value) -> str | None:
    """datetime/date/Timestamp/str -> 'YYYY-MM-DD' (None if it is not a date)."""
    if value is None:
        return None
    try:
        if hasattr(value, "date") and callable(value.date):
            value = value.date()
        text = value.isoformat()[:10] if hasattr(value, "isoformat") else str(value)[:10]
        datetime.fromisoformat(text)          # rejects "NaT", "nan", free text
        return text
    except (ValueError, TypeError, AttributeError):
        return None

# Thread pool for synchronous yfinance I/O
_pool = ThreadPoolExecutor(max_workers=4)


_FUND_TYPES = frozenset({"ETF", "MUTUALFUND", "INDEX", "CRYPTOCURRENCY", "CURRENCY", "FUTURE"})


def _is_fund(ticker) -> bool:
    """True for instruments that legitimately have no earnings (ETFs etc.); False when unknown."""
    try:
        info = ticker.info or {}
        return str(info.get("quoteType") or "").upper() in _FUND_TYPES
    except Exception:  # noqa: BLE001
        return False


class YahooFinanceService:
    """Yahoo Finance (unofficial, IP-throttled) for fundamentals and macro context only.

    Keep it OFF the trading hot path. Every call is bounded by ``yfinance_timeout_seconds``;
    failures are negative-cached for ``yfinance_negative_ttl_seconds``; after
    ``yfinance_breaker_failures`` consecutive failures a circuit breaker short-circuits all calls
    for ``yfinance_breaker_cooldown_seconds`` (M6). Failures return ``{"error": ...}`` as before.
    """

    def __init__(self, settings=None, *, clock=time.monotonic) -> None:
        if settings is None:
            try:
                from .config import load_settings
                settings = load_settings()
            except Exception:  # noqa: BLE001
                settings = None
        g = lambda name, default: getattr(settings, name, default)  # noqa: E731
        self.timeout = float(g("yfinance_timeout_seconds", 8.0))
        self.negative_ttl = float(g("yfinance_negative_ttl_seconds", 120.0))
        self.breaker_failures = int(g("yfinance_breaker_failures", 5))
        self.breaker_cooldown = float(g("yfinance_breaker_cooldown_seconds", 300.0))
        self._clock = clock
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._ttl_seconds: float = 300  # 5 minutes cache
        self._negative: dict[str, tuple[float, str]] = {}    # key -> (until, error)
        self._consecutive_failures = 0
        self._breaker_until = 0.0
        # Per-symbol earnings dates: key -> (monotonic expiry, [ISO dates]). Successful lookups
        # only; a failure is negative-cached by _guarded and never stored here.
        self.earnings_ttl = float(g("earnings_cache_hours", 12.0)) * 3600.0
        self._earnings: dict[str, tuple[float, list[str]]] = {}

    @property
    def breaker_open(self) -> bool:
        return self._clock() < self._breaker_until

    def _record_failure(self, key: str, err: str) -> None:
        now = self._clock()
        self._consecutive_failures += 1
        if self.negative_ttl > 0:
            self._negative[key] = (now + self.negative_ttl, err)
        if self._consecutive_failures >= self.breaker_failures:
            self._breaker_until = now + self.breaker_cooldown
            self._consecutive_failures = 0
            log.warning("yfinance circuit breaker open for %.0fs after %d consecutive failures",
                        self.breaker_cooldown, self.breaker_failures)

    async def _guarded(self, key: str, fn, *args) -> tuple[dict[str, Any] | None, str | None]:
        """Returns (data, None) on success or (None, error text)."""
        now = self._clock()
        neg = self._negative.get(key)
        if neg and now < neg[0]:
            return None, f"{neg[1]} (negative cache)"
        if now < self._breaker_until:
            return None, "yfinance circuit breaker open"
        loop = asyncio.get_running_loop()
        try:
            data = await asyncio.wait_for(loop.run_in_executor(_pool, fn, *args), self.timeout)
        except asyncio.TimeoutError:
            err = f"yfinance timed out after {self.timeout:g}s"
            self._record_failure(key, err)
            return None, err
        except Exception as e:  # noqa: BLE001
            err = str(e) or type(e).__name__
            self._record_failure(key, err)
            return None, err
        self._consecutive_failures = 0
        self._negative.pop(key, None)
        return data, None

    async def get_summary(self, symbol: str) -> dict[str, Any]:
        """Fetch fundamental summary, valuation ratios, analyst targets, and earnings calendar."""
        symbol = symbol.strip().upper()
        now = datetime.now(timezone.utc).timestamp()
        if symbol in self._cache:
            ts, cached = self._cache[symbol]
            if now - ts < self._ttl_seconds:
                return cached
        data, err = await self._guarded(symbol, self._fetch_sync, symbol)
        if data is None:
            log.warning("Failed to fetch Yahoo Finance data for %s: %s", symbol, err)
            return {"symbol": symbol, "error": err,
                    "fetched_at": datetime.now(timezone.utc).isoformat()}
        self._cache[symbol] = (now, data)
        return data

    async def get_earnings_dates(self, symbol: str) -> list[str] | None:
        """Earnings dates (YYYY-MM-DD, past and upcoming) for the blackout check, cached for
        ``earnings_cache_hours``. ``[]`` = Yahoo answered and lists none (ETFs, no schedule);
        ``None`` = could not be read (timeout, breaker, error) - callers fail closed on it."""
        symbol = symbol.strip().upper()
        hit = self._earnings.get(symbol)
        if hit and self._clock() < hit[0]:
            return list(hit[1])
        data, err = await self._guarded(f"earn:{symbol}", self._fetch_earnings_sync, symbol)
        if data is None:
            log.warning("Earnings dates for %s unavailable: %s", symbol, err)
            return None
        dates = list(data["dates"])
        if data.get("unknown"):
            # Empty calendar AND no history for a non-ETF: yfinance swallowed an HTTP error
            # (hide_exceptions). Unknown, not "no earnings": fail closed, and cache nothing.
            log.warning("Earnings dates for %s unreadable (empty answer from Yahoo)", symbol)
            return None
        self._earnings[symbol] = (self._clock() + self.earnings_ttl, dates)
        return list(dates)

    @staticmethod
    def _fetch_earnings_sync(symbol: str, ticker=None) -> dict[str, Any]:
        """Union of ``Ticker.calendar`` (next date, can be stale) and ``get_earnings_dates``
        (history + the next scheduled one). Raises only when BOTH sources fail."""
        import yfinance as yf

        ticker = ticker or yf.Ticker(symbol)
        dates: set[str] = set()
        errors: list[str] = []
        ok = 0
        try:
            cal = ticker.calendar
            ok += 1
            ed = cal.get("Earnings Date") if hasattr(cal, "get") else None
            for d in (ed if isinstance(ed, (list, tuple)) else [ed]):
                iso = _iso_date(d)
                if iso:
                    dates.add(iso)
        except Exception as e:  # noqa: BLE001
            errors.append(f"calendar: {e}")
        try:
            frame = ticker.get_earnings_dates(limit=12)
            ok += 1
            if frame is not None:
                for idx in getattr(frame, "index", []):
                    iso = _iso_date(idx)
                    if iso:
                        dates.add(iso)
        except Exception as e:  # noqa: BLE001
            errors.append(f"earnings_dates: {e}")
        if not ok:
            raise RuntimeError("; ".join(errors) or "no earnings source")
        if not dates and not _is_fund(ticker):
            return {"dates": [], "unknown": True}
        return {"dates": sorted(dates)}

    async def get_macro_context(self) -> dict[str, Any]:
        """Fetch broad market context indices (^VIX, ^GSPC, ^IXIC, ^TNX)."""
        cache_key = "_MACRO_"
        now = datetime.now(timezone.utc).timestamp()
        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if now - ts < 120:  # 2 minute cache for macro
                return cached
        data, err = await self._guarded(cache_key, self._fetch_macro_sync)
        if data is None:
            log.warning("Failed to fetch macro context from Yahoo Finance: %s", err)
            return {"error": err}
        self._cache[cache_key] = (now, data)
        return data

    def _fetch_sync(self, symbol: str) -> dict[str, Any]:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        fast = getattr(ticker, "fast_info", None)

        # Next earnings date: the calendar can lag (it showed a date 6 days in the past for NVDA
        # right after it reported), so take the first date >= today from calendar + history.
        earnings_date = None
        try:
            found = self._fetch_earnings_sync(symbol, ticker)["dates"]
            today = datetime.now(timezone.utc).date().isoformat()
            future = [d for d in found if d >= today]
            earnings_date = future[0] if future else None
        except Exception:
            pass
        try:
            from .news_events import register_name
            register_name(symbol, info.get("longName") or info.get("shortName"))
        except Exception:  # noqa: BLE001
            pass

        # Target mean price
        target_mean = info.get("targetMeanPrice")
        recommendation = info.get("recommendationKey") or info.get("recommendationMean")

        return {
            "symbol": symbol,
            "name": info.get("shortName") or info.get("longName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": info.get("marketCap") or (fast.market_cap if fast else None),
            "pe_trailing": info.get("trailingPE"),
            "pe_forward": info.get("forwardPE"),
            "peg_ratio": info.get("pegRatio"),
            "price_to_book": info.get("priceToBook"),
            "beta": info.get("beta"),
            "profit_margins": info.get("profitMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh") or (fast.year_high if fast else None),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow") or (fast.year_low if fast else None),
            "target_mean_price": target_mean,
            "target_high": info.get("targetHighPrice"),
            "target_low": info.get("targetLowPrice"),
            "recommendation": recommendation,
            "number_of_analyst_opinions": info.get("numberOfAnalystOpinions"),
            "earnings_date": earnings_date,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _fetch_macro_sync(self) -> dict[str, Any]:
        import yfinance as yf

        macro_syms = {
            "VIX": "^VIX",
            "SP500": "^GSPC",
            "NASDAQ": "^IXIC",
            "US10Y": "^TNX",
        }
        res: dict[str, Any] = {}
        for key, sym in macro_syms.items():
            try:
                t = yf.Ticker(sym)
                fast = getattr(t, "fast_info", None)
                if fast:
                    res[key] = {
                        "symbol": sym,
                        "price": float(fast.last_price or 0.0),
                        "prev_close": float(fast.previous_close or 0.0),
                    }
            except Exception as e:
                res[key] = {"symbol": sym, "error": str(e)}
        return res
