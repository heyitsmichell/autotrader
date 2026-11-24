"""TickBus: per-symbol RingBuffer, incremental BarAggregator (1m/5m) and throttled quote events.

Pure in-memory and thread-agnostic for reads (``publish`` is called from the event loop only, via
``MarketFeed._publish``). No network.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Iterable

from ..eventhub import hub
from ..quotes import Quote

log = logging.getLogger(__name__)

INTERVALS = {"1m": 60, "5m": 300}
QUOTE_EVENT_MIN_GAP = 0.25          # at most 4 "quote" events per second per symbol


class RingBuffer:
    """Fixed-size buffer of the most recent ticks for one symbol (tick = {"symbol","t","p","v"})."""

    def __init__(self, maxlen: int = 2000) -> None:
        self._d: deque[dict] = deque(maxlen=max(1, int(maxlen)))

    def append(self, tick: dict) -> None:
        self._d.append(tick)

    def __len__(self) -> int:
        return len(self._d)

    def latest(self) -> dict | None:
        return self._d[-1] if self._d else None

    def ticks(self, n: int | None = None) -> list[dict]:
        items = list(self._d)
        return items if n is None else items[-n:] if n > 0 else []

    @property
    def maxlen(self) -> int:
        return self._d.maxlen or 0


class BarAggregator:
    """Builds OHLCV bars incrementally from ticks. O(1) per tick; keeps ``keep`` closed bars.

    Bar dicts: {"t": bar start (epoch s, aligned to the interval), "open","high","low","close","volume"}.
    Out-of-order ticks older than the current bar are ignored (the ring still has them).
    """

    def __init__(self, interval_seconds: int, keep: int = 1000) -> None:
        self.interval = int(interval_seconds)
        self._closed: deque[dict] = deque(maxlen=keep)
        self._cur: dict | None = None

    def add(self, ts: float, price: float, volume: float = 0.0) -> None:
        start = int(ts // self.interval) * self.interval
        cur = self._cur
        if cur is None or start > cur["t"]:
            if cur is not None:
                self._closed.append(cur)
            self._cur = {"t": start, "open": price, "high": price, "low": price,
                         "close": price, "volume": float(volume or 0.0)}
            return
        if start < cur["t"]:
            return
        cur["high"] = max(cur["high"], price)
        cur["low"] = min(cur["low"], price)
        cur["close"] = price
        cur["volume"] += float(volume or 0.0)

    def bars(self, n: int | None = None) -> list[dict]:
        out = [dict(b) for b in self._closed]
        if self._cur is not None:
            out.append(dict(self._cur))
        return out if n is None else out[-n:] if n > 0 else []


def budget_subscriptions(held: Iterable[str], candidates: dict[str, list[str]],
                         top_k: int, max_subscriptions: int) -> list[str]:
    """Symbols to stream: every held symbol first, then the top ``top_k`` candidates per
    underlying (in the order given), truncated to ``max_subscriptions``. Deterministic."""
    out: list[str] = []
    seen: set[str] = set()

    def add(sym: str) -> None:
        sym = str(sym).strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)

    for sym in sorted({str(h).strip().upper() for h in held if str(h).strip()}):
        add(sym)
    n_held = len(out)
    for underlying in sorted(candidates):
        for sym in list(candidates[underlying])[:max(0, int(top_k))]:
            add(sym)
    if len(out) > max_subscriptions:
        if n_held > max_subscriptions:
            log.warning("feed: %d held symbols exceed feed_max_subscriptions=%d; truncating",
                        n_held, max_subscriptions)
        out = out[:max_subscriptions]
    return out


class TickBus:
    def __init__(self, ring_size: int = 2000) -> None:
        self.ring_size = ring_size
        self._rings: dict[str, RingBuffer] = {}
        self._aggs: dict[str, dict[str, BarAggregator]] = {}
        self._quotes: dict[str, Quote] = {}
        self._last_event: dict[str, float] = {}

    def ring(self, symbol: str) -> RingBuffer:
        symbol = symbol.upper()
        r = self._rings.get(symbol)
        if r is None:
            r = self._rings[symbol] = RingBuffer(self.ring_size)
        return r

    def bars(self, symbol: str, interval: str = "1m", n: int | None = None) -> list[dict]:
        if interval not in INTERVALS:
            raise ValueError(f"unsupported interval {interval!r}; use one of {sorted(INTERVALS)}")
        agg = self._aggs.get(symbol.upper(), {}).get(interval)
        return agg.bars(n) if agg else []

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol.upper())

    def publish(self, tick: dict, source: str = "stream", *, now: float | None = None) -> None:
        sym = str(tick["symbol"]).upper()
        self.ring(sym).append(tick)
        aggs = self._aggs.get(sym)
        if aggs is None:
            aggs = self._aggs[sym] = {k: BarAggregator(v) for k, v in INTERVALS.items()}
        for agg in aggs.values():
            agg.add(tick["t"], tick["p"], tick.get("v", 0.0))
        q = Quote(symbol=sym, last=tick["p"], bid=tick.get("bid"), ask=tick.get("ask"),
                  as_of=tick["t"], source=source)
        self._quotes[sym] = q
        t = time.time() if now is None else now
        if t - self._last_event.get(sym, 0.0) >= QUOTE_EVENT_MIN_GAP:
            self._last_event[sym] = t
            hub.publish("quote", {"symbol": sym, "price": q.last, "bid": q.bid, "ask": q.ask,
                                  "ts": tick["t"], "as_of": q.as_of})
