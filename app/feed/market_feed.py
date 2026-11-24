"""Live prices for the chart.

Two sources:
- Streaming (Webull mode): the Webull SDK's DataStreamingClient pushes trades
  and snapshots over MQTT. This is true real-time push. It runs outside the MCP
  server, because MCP is request/response only.
- Polling (mock mode, or fallback): asks the MCP server for a snapshot every
  POLL_SECONDS. In streaming mode it only polls symbols with no recent push
  (e.g. outside market hours or if a symbol never ticks).

Prices are only fetched for symbols someone is watching on the chart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict
from types import SimpleNamespace

from ..broker import Broker, BrokerError, parse_snapshot  # noqa: F401
from ..config import ROOT, Settings
from ..quotes import Quote
from .bus import RingBuffer, TickBus, budget_subscriptions

log = logging.getLogger(__name__)

# From https://developer.webull.com/apis/docs/market-data-api/getting-started
US_STREAM_HOSTS = {
    "prod": ("api.webull.com", "data-api.webull.com"),
    "uat": ("api.sandbox.webull.com", "data-api.sandbox.webull.com"),
}
STALE_SECONDS = 15


class MarketFeed:
    def __init__(self, settings: Settings, broker: Broker) -> None:
        self.s = settings
        self.broker = broker
        self.mode = "off"            # "stream" | "poll" | "off"
        self.error: str | None = None
        self.last: dict[str, dict] = {}
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._prev_volume: dict[str, float] = {}
        self._poll_task: asyncio.Task | None = None
        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Stream failure handling
        self._stream_failures = 0
        self._max_stream_failures = 3
        self._stream_backoff_until: float = 0.0
        self._stream_backoff_seconds = 300  # 5 minutes backoff
        self._negative_cache: dict[str, tuple[float, float]] = {}  # endpoint -> (fail_count, cached_until)
        self._negative_cache_ttl = 60.0  # seconds to cache failures
        # Hot-path state (contracts s5): ticks -> ring buffers -> incremental bars -> quotes
        self.bus = TickBus(int(getattr(settings, "feed_ring_size", 2000)))
        self._stream_symbols: list[str] = list(settings.watchlist)   # MQTT subscription set
        self._stream_category = None

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        src = self.s.realtime_source
        # Webull's sandbox accepts request/response market-data calls but does
        # not provide the MQTT stream used by the SDK. Keep auto mode reliable
        # there; an explicit REALTIME_SOURCE=stream still lets production users
        # opt in and receive the normal fallback error if their subscription is
        # unavailable.
        want_stream = src == "stream" or (
            src == "auto" and self.s.broker_mode == "webull"
            and self.s.webull_environment == "prod"
        )
        self.mode = "poll"
        if want_stream:
            # Check if we're in stream backoff period
            now = time.time()
            if now < self._stream_backoff_until:
                remaining = int(self._stream_backoff_until - now)
                log.warning(f"Stream in backoff period ({remaining}s remaining). Starting in poll mode.")
                self.error = f"Stream failed {self._max_stream_failures} times; backoff active ({remaining}s). Polling every {self.s.poll_seconds:g}s."
            else:
                try:
                    await self._start_stream()
                    self.mode = "stream"
                    self.error = None
                    self._stream_failures = 0  # Reset on successful connection
                except Exception as e:  # noqa: BLE001 - any failure falls back to polling
                    await self._stop_stream()
                    self._stream_failures += 1
                    if self._stream_failures >= self._max_stream_failures:
                        self._stream_backoff_until = now + self._stream_backoff_seconds
                        self.error = (f"Stream failed {self._stream_failures} times; "
                                      f"backing off for {self._stream_backoff_seconds}s. "
                                      f"Polling every {self.s.poll_seconds:g}s. "
                                      f"Set REALTIME_SOURCE=poll to disable stream attempts.")
                        log.warning(self.error)
                    else:
                        self.error = (f"Streaming unavailable ({e}). Polling every {self.s.poll_seconds:g}s. "
                                      f"Attempt {self._stream_failures}/{self._max_stream_failures}.")
                        log.warning(self.error)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="price-poll")

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        await self._stop_stream()
        self.mode = "off"

    # -- subscribers (one per open chart) -------------------------------------
    def subscribe(self, symbol: str, q: asyncio.Queue | None = None) -> asyncio.Queue:
        """Watch a symbol. Pass the same queue for several symbols to get one stream."""
        q = q or asyncio.Queue(maxsize=500)
        self._subs[symbol].add(q)
        return q

    def unsubscribe(self, symbol: str, q: asyncio.Queue) -> None:
        self._subs[symbol].discard(q)

    def _publish(self, symbol: str, ts: float, price: float, volume: float,
                 source: str | None = None) -> None:
        tick = {"symbol": symbol, "t": ts, "p": price, "v": volume}
        self.last[symbol] = tick
        try:
            self.bus.publish(tick, source or ("stream" if self.mode == "stream" else "poll"))
        except Exception:  # noqa: BLE001 - the chart stream must never break on bus errors
            log.debug("tick bus publish failed", exc_info=True)
        for q in list(self._subs.get(symbol, ())):
            try:
                q.put_nowait(tick)
            except asyncio.QueueFull:
                pass

    # -- hot-path accessors (contracts s5) -------------------------------------
    def quote(self, symbol: str) -> Quote | None:
        """Latest tick as a ``Quote`` (``as_of`` = tick time), or None if never seen."""
        return self.bus.quote(symbol)

    def ring(self, symbol: str) -> RingBuffer:
        return self.bus.ring(symbol)

    def bars(self, symbol: str, interval: str = "1m", n: int | None = None) -> list[dict]:
        """Incrementally aggregated bars ("1m" or "5m"), oldest first; last one is still forming."""
        return self.bus.bars(symbol, interval, n)

    @property
    def subscriptions(self) -> list[str]:
        return list(self._stream_symbols)

    def set_subscriptions(self, held: set[str], candidates: dict[str, list[str]]) -> list[str]:
        """Apply the subscription budget ``held U top_k_per_underlying <= feed_max_subscriptions``
        and re-subscribe the MQTT topics (when streaming). Returns the resulting symbol list."""
        target = budget_subscriptions(held, candidates, int(getattr(self.s, "feed_options_top_k", 4)),
                                      int(getattr(self.s, "feed_max_subscriptions", 100)))
        old, new = set(self._stream_symbols), set(target)
        self._stream_symbols = target
        if self._client is not None and (old != new):
            self._schedule(self._resubscribe(sorted(new - old), sorted(old - new)))
        return target

    def _schedule(self, coro) -> None:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or loop.is_closed():
            coro.close()
        elif running is loop:
            loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)

    @staticmethod
    def _split_by_category(symbols: list[str]) -> dict[str, list[str]]:
        import re
        out: dict[str, list[str]] = {"US_STOCK": [], "US_OPTION": []}
        for s in symbols:
            out["US_OPTION" if re.fullmatch(r"[A-Z]{1,6}\d{6}[CP]\d{8}", s) else "US_STOCK"].append(s)
        return out

    async def _resubscribe(self, add: list[str], remove: list[str]) -> None:
        client = self._client
        if client is None:
            return
        from webull.data.common.subscribe_type import SubscribeType
        types = [SubscribeType.SNAPSHOT.name, SubscribeType.TICK.name]
        try:
            for cat, syms in self._split_by_category(remove).items():
                if syms:
                    await asyncio.to_thread(client.unsubscribe, syms, cat, types)
            for cat, syms in self._split_by_category(add).items():
                if syms:
                    await asyncio.to_thread(client.subscribe, syms, cat, types)
        except Exception as e:  # noqa: BLE001 - keep the old set running; polling covers gaps
            log.warning("feed re-subscribe failed: %s", e)

    # -- polling ---------------------------------------------------------------
    async def _poll_loop(self) -> None:
        while True:
            now = time.time()
            symbols = sorted(
                s for s, qs in self._subs.items() if qs and (
                    self.mode != "stream"
                    or now - self.last.get(s, {}).get("t", 0) > STALE_SECONDS)
            )
            if symbols and self.broker.connected:
                try:
                    # Use batched fetch with TTL cache from broker
                    snaps = await self.broker._fetch_snapshots(symbols)
                    for sym, snap in snaps.items():
                        if snap.get("price") is None or sym not in symbols:
                            continue
                        vol = 0.0
                        if snap.get("volume") is not None:
                            prev = self._prev_volume.get(sym)
                            if prev is not None and snap["volume"] >= prev:
                                vol = snap["volume"] - prev
                            self._prev_volume[sym] = snap["volume"]
                        self._publish(sym, time.time(), snap["price"], vol)
                except BrokerError as e:
                    log.debug("poll failed: %s", e)
            await asyncio.sleep(self.s.poll_seconds)

    # -- Webull streaming (MQTT) -----------------------------------------------
    async def _start_stream(self) -> None:
        from webull.data.common.subscribe_type import SubscribeType
        from webull.data.data_streaming_client import DataStreamingClient

        key, secret = os.environ.get("WEBULL_APP_KEY"), os.environ.get("WEBULL_APP_SECRET")
        if not key or not secret:
            raise RuntimeError("WEBULL_APP_KEY / WEBULL_APP_SECRET not set")
        if self.s.webull_region != "us":
            raise RuntimeError("streaming hosts are only configured for the US region")
        env = self.s.webull_environment
        http_host, mqtt_host = US_STREAM_HOSTS[env]

        loop = self._loop
        connected = asyncio.Event()
        # A unique session id per connection (Webull drops an older connection
        # that reuses one; each App Key allows 5 concurrent connections).
        client = DataStreamingClient(key, secret, "us", f"trader{uuid.uuid4().hex[:20]}",
                                     http_host=http_host, mqtt_host=mqtt_host)
        token_dir = os.environ.get("WEBULL_TOKEN_DIR") or "conf"
        client.set_token_dir(str((ROOT / token_dir).resolve()))  # share the token from auth.ps1
        if env == "uat":
            from webull_openapi_mcp.sdk_client import _configure_uat_endpoints
            _configure_uat_endpoints(client.api_client,
                                     SimpleNamespace(environment="uat", region_id="us"))

        def on_connect(c, api_client, session_id):
            # Also runs after every reconnect, which must re-subscribe (per Webull docs).
            types = [SubscribeType.SNAPSHOT.name, SubscribeType.TICK.name]
            for cat, syms in self._split_by_category(list(self._stream_symbols)).items():
                if syms:
                    c.subscribe(syms, cat, types)
            loop.call_soon_threadsafe(connected.set)

        def on_message(c, topic, quote):
            try:
                price = getattr(quote, "price", None)
                if price is None:
                    return
                basic = quote.basic
                ts = int(basic.timestamp) / 1000 if basic.timestamp else time.time()
                vol = float(quote.volume) if topic == "tick" and quote.volume else 0.0
                loop.call_soon_threadsafe(self._publish, str(basic.symbol).upper(), ts,
                                          float(price), vol)
            except Exception:  # noqa: BLE001 - never kill the MQTT thread
                log.exception("bad streaming message on %s", topic)

        client.on_connect_success = on_connect
        client.on_quotes_message = on_message
        self._client = client
        client.connect_and_loop_start()  # runs in a daemon thread
        try:
            await asyncio.wait_for(connected.wait(), 30)
        except asyncio.TimeoutError:
            raise RuntimeError("no streaming connection within 30s; check your OpenAPI "
                               "market data subscription and the Webull token") from None

    async def _stop_stream(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        client._thread_terminate = True  # stop the SDK's retry loop
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(client.loop_stop), 5)
        except Exception:  # noqa: BLE001 - daemon thread; it dies with the process
            pass

    # -- negative cache for failed broker endpoints -------------------------------
    def _record_negative_cache(self, key: str, now: float) -> None:
        """Record a failure for an endpoint. After max failures, cache it."""
        fail_count, _ = self._negative_cache.get(key, (0, 0.0))
        fail_count += 1
        if fail_count >= 3:  # Cache after 3 consecutive failures
            self._negative_cache[key] = (fail_count, now + self._negative_cache_ttl)
        else:
            self._negative_cache[key] = (fail_count, 0.0)

    def _clear_negative_cache(self, key: str) -> None:
        """Clear negative cache on success."""
        if key in self._negative_cache:
            del self._negative_cache[key]

    def _is_cached_failure(self, key: str, now: float) -> bool:
        """Check if endpoint is in negative cache (cached failure)."""
        fail_count, cached_until = self._negative_cache.get(key, (0, 0.0))
        if cached_until > now:
            return True
        # Expired cache - clean up
        if cached_until > 0 and cached_until <= now:
            del self._negative_cache[key]
        return False
