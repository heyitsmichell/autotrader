"""Read-only market data and account reads (mixin for ``Broker``)."""

from __future__ import annotations

import asyncio
import logging
import time

from ..market import order_session
from ..quotes import Quote
from .errors import BrokerError
from .parsers import (STOCK_ACCOUNT_CLASSES, parse_accounts, parse_bars, parse_day_pnl,
                      live_price_for_session, parse_positions, parse_snapshot)

log = logging.getLogger(__name__)


class MarketDataMixin:
    def _init_market_data(self) -> None:
        # Snapshot cache: symbol -> (timestamp, snapshot_data)
        self._snapshot_cache: dict[str, tuple[float, dict]] = {}
        self._snapshot_cache_ttl = 10
        # Serialises snapshot fetches so simultaneous callers share one request via the cache.
        self._snapshot_lock: asyncio.Lock | None = None

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
                text = await self.call("get_stock_snapshot", symbols=symbols_str)
                snaps = parse_snapshot(text)
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

    async def positions_text(self) -> str:
        return await self.call("get_account_positions", account_id=await self.account_id())

    async def open_orders_text(self) -> str:
        return await self.call("get_open_orders", account_id=await self.account_id())

    async def positions(self) -> dict[str, float]:
        text = await self.positions_text()
        return {} if text.startswith("No data") else parse_positions(text)

    async def day_pnl(self) -> float | None:
        return parse_day_pnl(await self.balance_text())

    async def daily_bars(self, symbol: str, count: int = 120) -> list[dict]:
        return parse_bars(await self.call("get_stock_bars", symbols=symbol, timespan="D",
                                          count=count))

    async def intraday_bars(self, symbol: str, timespan: str = "5m", count: int = 390) -> list[dict]:
        """timespan: "1m", "5m", "15m", "30m", "60m"; count: number of bars (max 1000)."""
        return parse_bars(await self.call("get_stock_bars", symbols=symbol, timespan=timespan,
                                          count=min(count, 1000)))

    async def order_history_text(self) -> str:
        return await self.call("get_order_history", account_id=await self.account_id())
