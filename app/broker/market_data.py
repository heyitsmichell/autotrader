"""Read-only market data and account reads (mixin for ``Broker``)."""

from __future__ import annotations

import asyncio
import logging
import time

from ..market import order_session
from ..quotes import Quote
from .errors import BrokerError
from .parsers import (STOCK_ACCOUNT_CLASSES, parse_accounts, parse_bars, parse_bars_by_symbol, parse_day_pnl,
                      live_price_for_session, parse_positions, parse_snapshot)

log = logging.getLogger(__name__)


async def fetch_bars_many(broker, symbols, count: int, *, errors: dict | None = None,
                          sink: dict | None = None, use_cache: bool = True) -> dict[str, list[dict]]:
    """Batch daily bars via ``broker.daily_bars_many``; brokers without it (test fakes, backtest
    adapters) get one ``daily_bars`` call per symbol."""
    many = getattr(broker, "daily_bars_many", None)
    if many is not None:
        return await many(list(symbols), count, errors=errors, sink=sink, use_cache=use_cache)
    out = sink if sink is not None else {}
    for sym in dict.fromkeys(s.upper() for s in symbols):
        try:
            out[sym] = await broker.daily_bars(sym, count)
        except BrokerError as e:
            if errors is None:
                log.warning("bars for %s failed: %s", sym, e)
            else:
                errors[sym] = e
    return out


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing-only host contract; no runtime base class
    from ._host import MarketDataHost as _Host
else:
    _Host = object

class MarketDataMixin(_Host):
    def _init_market_data(self) -> None:
        # Snapshot cache: symbol -> (timestamp, snapshot_data)
        self._snapshot_cache: dict[str, tuple[float, dict]] = {}
        self._snapshot_cache_ttl = 10
        # Serialises snapshot fetches so simultaneous callers share one request via the cache.
        self._snapshot_lock: asyncio.Lock | None = None
        self._init_load_caches()

    def _init_load_caches(self) -> None:
        # Positions: short TTL + single-flight. Fills are invisible to this class, so the 3 s TTL
        # is what bounds staleness after a fill; orders call invalidate_positions() explicitly.
        self._pos_cache: tuple[float, str] | None = None
        self._pos_gen = 0
        self._pos_lock: asyncio.Lock | None = None
        # Shared daily-bars cache: symbol -> (monotonic ts, requested count, bars).
        self._bars_shared: dict[str, tuple[float, int, list[dict]]] = {}

    async def _fetch_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch snapshots for multiple symbols in a single batch call.

        Returns {symbol: {"price" (current-session live price), "regular_price", "ext_price",
        "ovn_price", "stale", "price_source", "volume", "prev_close", "as_of"}}. Uses a TTL cache to avoid
        redundant calls. ``as_of`` is the fetch time (the API has no per-quote timestamp).
        """
        if self._snapshot_lock is None:
            self._snapshot_lock = asyncio.Lock()
        result: dict[str, dict] = {}
        wanted = [sym.upper() for sym in symbols]

        def _from_cache() -> list[str]:
            now = time.time()
            missing: list[str] = []
            for sym in wanted:
                cached = self._snapshot_cache.get(sym)
                if cached and now - cached[0] < self._snapshot_cache_ttl:
                    result[sym] = cached[1]
                else:
                    missing.append(sym)
            return missing

        if not _from_cache():
            return result

        # One batch at a time. Callers that waited re-check the cache, so 17 symbols asked for
        # at once become one request instead of 17 (the cause of the 429s).
        async with self._snapshot_lock:
            uncached = _from_cache()
            if not uncached:
                return result
            symbols_str = ",".join(dict.fromkeys(uncached))
            try:
                # Without these flags Webull returns only the regular-session lines, so outside
                # CORE every quote looked stale: extended-hours BUYs were all refused and the
                # soft stop (which needs a fresh quote) could never fire. Verified live 2026-11-24.
                text = await self.call("get_stock_snapshot", symbols=symbols_str,
                                       extend_hour_required=True, overnight_required=True)
                snaps = parse_snapshot(text, wanted)
                stamp = time.time()
                session = order_session()
                is_mock = getattr(getattr(self, "settings", None), "broker_mode", "") == "mock"
                for sym, data in snaps.items():
                    # `price` becomes the CURRENT-session price (ExtHr premarket/after-hours, OVN
                    # overnight); the regular-session price stays in `regular_price`. `stale` marks
                    # a regular/previous close used outside CORE because no extended print exists.
                    live, source, stale = live_price_for_session(data, session)
                    if is_mock:     # the mock server has no extended-hours lines
                        stale = False
                    data = {**data, "as_of": stamp, "session": session, "price_source": source,
                            "stale": stale}
                    if live is not None:
                        data["price"] = live
                    self._snapshot_cache[sym] = (stamp, data)
                    result[sym] = data
            except BrokerError as e:
                # On error, return whatever we have (may be partial); callers handle missing data
                log.warning(f"Batch snapshot fetch failed: {e}")

        return result

    async def last_price(self, symbol: str) -> float | None:
        """Get last price for a symbol, using cached snapshot if available."""
        snaps = await self._fetch_snapshots([symbol])
        snap = snaps.get(symbol.upper())
        if snap and snap.get("price") is not None:
            return float(snap["price"])
        return None

    async def is_price_stale(self, symbol: str) -> bool:
        """True outside CORE when only the previous regular close exists (no extended print)."""
        snap = (await self._fetch_snapshots([symbol])).get(symbol.upper())
        return bool(snap and snap.get("stale"))

    async def get_snapshot(self, symbol: str) -> dict | None:
        """Get full snapshot data for a symbol (price, volume, prev_close, as_of)."""
        snaps = await self._fetch_snapshots([symbol])
        return snaps.get(symbol.upper())

    async def quote(self, symbol: str) -> Quote | None:
        """Latest equity quote as a ``Quote`` (``last`` + ``as_of``). The snapshot endpoint has no
        bid/ask, so those stay None (``two_sided`` is False)."""
        snap = await self.get_snapshot(symbol)
        if not snap or snap.get("price") is None:
            return None
        return Quote(symbol=symbol.upper(), last=float(snap["price"]),
                     as_of=float(snap.get("as_of") or time.time()),
                     source="mock" if self.settings.broker_mode == "mock" else "snapshot",
                     volume=snap.get("volume"), stale=bool(snap.get("stale")))

    async def account_id(self) -> str:
        if self._account_id:
            return self._account_id
        text = await self.call("get_account_list")
        accounts = parse_accounts(text)
        if not accounts:
            raise BrokerError(f"No account found in get_account_list output: {text[:300]}")
        stock = [a for a in accounts if a["account_class"] in STOCK_ACCOUNT_CLASSES]
        self._account_id = (stock or accounts)[0]["account_id"]
        return self._account_id

    async def balance_text(self) -> str:
        return await self.call("get_account_balance", account_id=await self.account_id())

    async def account_balance(self) -> dict:
        """Parsed account balance (net_liq, cash, market_value, day_pnl...)."""
        from ..portfolio import parse_balance
        return parse_balance(await self.balance_text())

    POSITIONS_TTL_SECONDS = 3.0
    BARS_SHARED_TTL_SECONDS = 900.0
    BARS_BATCH_SIZE = 20     # SDK documents no cap for get_batch_history_bar; 20 x 260 bars is small

    def invalidate_positions(self) -> None:
        """Drop the cached positions (called around every order/cancel/stop)."""
        d = self.__dict__
        d["_pos_gen"] = d.get("_pos_gen", 0) + 1
        d["_pos_cache"] = None

    async def positions_text(self, fresh: bool = False) -> str:
        """Raw positions. Cached POSITIONS_TTL_SECONDS with single-flight; ``fresh=True`` bypasses
        the cache (the result is still stored for others) for callers that need post-fill truth."""
        d = self.__dict__
        if "_pos_lock" not in d:
            self._init_load_caches()
        if d["_pos_lock"] is None:
            d["_pos_lock"] = asyncio.Lock()

        def _hit() -> str | None:
            c = d["_pos_cache"]
            if not fresh and c and time.monotonic() - c[0] < self.POSITIONS_TTL_SECONDS:
                return c[1]
            return None

        if (text := _hit()) is not None:
            return text
        async with d["_pos_lock"]:
            if (text := _hit()) is not None:
                return text
            gen = d["_pos_gen"]
            text = await self.call("get_account_positions", account_id=await self.account_id())
            if gen == d["_pos_gen"]:      # an order invalidated mid-flight: do not cache
                d["_pos_cache"] = (time.monotonic(), text)
            return text

    async def open_orders_text(self) -> str:
        return await self.call("get_open_orders", account_id=await self.account_id())

    async def positions(self, fresh: bool = False) -> dict[str, float]:
        """Shares held per symbol, as the swing engine owns them: the shares the multi-strategy
        sleeves hold (``reserved_positions``, set by the engine) are subtracted, so stops, exits,
        flatten and reconcile never touch sleeve shares (docs/SLEEVES_ARCHITECTURE.md section 2)."""
        raw = await self.positions_raw(fresh=fresh)
        return subtract_reserved(raw, self.__dict__.get("reserved_positions"))

    async def positions_raw(self, fresh: bool = False) -> dict[str, float]:
        """Every share the broker reports, sleeves included."""
        text = await self.positions_text(fresh=fresh)
        return {} if text.startswith("No data") else parse_positions(text)

    async def day_pnl(self) -> float | None:
        return parse_day_pnl(await self.balance_text())

    def _shared_bars_get(self, sym: str, count: int) -> list[dict] | None:
        hit = self.__dict__.get("_bars_shared", {}).get(sym)
        if hit and hit[1] >= count and time.monotonic() - hit[0] < self.BARS_SHARED_TTL_SECONDS:
            return hit[2][-count:]
        return None

    def _shared_bars_put(self, sym: str, count: int, bars: list[dict]) -> None:
        cache = self.__dict__.setdefault("_bars_shared", {})
        old = cache.get(sym)
        expired = not old or time.monotonic() - old[0] >= self.BARS_SHARED_TTL_SECONDS
        if bars and (expired or count >= old[1]):     # a small incremental fetch never shrinks it
            cache[sym] = (time.monotonic(), count, bars)

    async def daily_bars(self, symbol: str, count: int = 120, *, use_cache: bool = True) -> list[dict]:
        sym = symbol.upper()
        if use_cache and (hit := self._shared_bars_get(sym, count)) is not None:
            return hit
        bars = parse_bars(await self.call("get_stock_bars", symbols=symbol, timespan="D",
                                          count=count))
        self._shared_bars_put(sym, count, bars)
        return bars

    async def daily_bars_many(self, symbols: list[str], count: int = 120, *,
                              use_cache: bool = True, errors: dict | None = None,
                              sink: dict | None = None) -> dict[str, list[dict]]:
        """Daily bars for many symbols using the batch endpoint (BARS_BATCH_SIZE per call).

        Symbols missing from a batch reply (or whose batch failed) are fetched singly. Per-symbol
        failures go to ``errors`` (symbol -> exception) when given, else are logged and skipped.
        ``sink`` receives results as each chunk completes (partial results survive a deadline).
        """
        out = sink if sink is not None else {}
        syms = list(dict.fromkeys(s.upper() for s in symbols))
        need = []
        for sym in syms:
            hit = self._shared_bars_get(sym, count) if use_cache else None
            if hit is not None:
                out[sym] = hit
            else:
                need.append(sym)
        n = max(1, self.BARS_BATCH_SIZE)
        for i in range(0, len(need), n):
            chunk = need[i:i + n]
            groups: dict = {}
            try:
                groups = parse_bars_by_symbol(await self.call(
                    "get_stock_bars", symbols=",".join(chunk), timespan="D", count=count))
            except BrokerError as e:
                log.info("batch bars for %d symbols failed (%s); falling back to single calls",
                         len(chunk), e)
            if None in groups and len(chunk) == 1 and chunk[0] not in groups:
                groups[chunk[0]] = groups.pop(None)
            for sym in chunk:
                if sym in groups:
                    out[sym] = groups[sym]
                    self._shared_bars_put(sym, count, groups[sym])
                    continue
                try:
                    out[sym] = await self.daily_bars(sym, count, use_cache=False)
                except BrokerError as e:
                    if errors is None:
                        log.warning("bars for %s failed: %s", sym, e)
                    else:
                        errors[sym] = e
        return out

    async def intraday_bars(self, symbol: str, timespan: str = "5m", count: int = 390) -> list[dict]:
        """timespan: "1m", "5m", "15m", "30m", "60m"; count: number of bars (max 1000)."""
        return parse_bars(await self.call("get_stock_bars", symbols=symbol, timespan=timespan,
                                          count=min(count, 1000)))

    async def order_history_text(self) -> str:
        return await self.call("get_order_history", account_id=await self.account_id())


def subtract_reserved(raw: dict[str, float], provider) -> dict[str, float]:
    """``raw`` minus the sleeve-reserved shares from ``provider()``; symbols left at 0 drop out.
    A provider failure leaves ``raw`` unchanged (logged): hiding swing shares would be worse."""
    if provider is None:
        return raw
    try:
        reserved = provider() or {}
    except Exception:  # noqa: BLE001
        log.warning("sleeve reserved positions unavailable; using raw broker positions", exc_info=True)
        return raw
    if not reserved:
        return raw
    out: dict[str, float] = {}
    for sym, qty in raw.items():
        left = qty - float(reserved.get(sym, 0.0)) if qty > 0 else qty
        if qty > 0 and left < 1e-9:
            continue
        out[sym] = left
    return out


async def fresh_positions(broker) -> dict[str, float]:
    """Positions straight from the broker, bypassing the short TTL cache, for callers that size or
    verify orders (flatten, exits, chase, reconcile, risk). Falls back to a plain call for brokers
    (fakes, adapters) whose positions() takes no ``fresh`` argument."""
    try:
        return await broker.positions(fresh=True)
    except TypeError:
        return await broker.positions()
