"""Server-Sent Events: the multiplexed EventHub stream and the per-symbol chart tick stream.

``GET /api/stream``                      -> multiplexed hub stream (see docs/API_STREAM.md)
``GET /api/stream?symbol=X|symbols=A,B`` -> chart ticks (MarketChart compatibility)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..eventhub import hub
from .chart import chart_symbol
from .portfolio import build_portfolio
from .proposals import list_proposals
from .status import build_status

log = logging.getLogger("trader")

router = APIRouter()

PUBLISH_INTERVAL = 2.0
CHART_KEEPALIVE = 15.0
# Fields that change on every snapshot without carrying news; ignored when diffing.
VOLATILE_KEYS = {"status": ("server_time", "now_et", "data_age_seconds")}
# Event types that are never diffed (each occurrence is meaningful).
NO_DIFF = {"heartbeat", "fill"}


def fingerprint(type_: str, data: Any) -> str:
    if isinstance(data, dict) and type_ in VOLATILE_KEYS:
        data = {k: v for k, v in data.items() if k not in VOLATILE_KEYS[type_]}
    return json.dumps(data, sort_keys=True, default=str)


def sse(type_: str, data: Any) -> str:
    return f"event: {type_}\ndata: {json.dumps(data, default=str)}\n\n"


async def snapshots(st) -> dict[str, Any]:
    """Fresh status / portfolio / proposals. A part that fails is left out, never raised."""
    out: dict[str, Any] = {}
    try:
        out["status"] = await build_status(st)
    except Exception:  # noqa: BLE001
        log.debug("stream: status snapshot failed", exc_info=True)
    try:
        if st.broker.connected:
            out["portfolio"] = await build_portfolio(st)
    except Exception:  # noqa: BLE001 - HTTPException(409) or a broker error
        log.debug("stream: portfolio snapshot failed", exc_info=True)
    try:
        out["proposals"] = list_proposals(st)
    except Exception:  # noqa: BLE001
        log.debug("stream: proposals snapshot failed", exc_info=True)
    return out


async def publisher_loop(app, interval: float = PUBLISH_INTERVAL) -> None:
    """Snapshot status / portfolio / proposals every ``interval`` s; publish only on change.
    Idle (no subscribers) ticks are skipped so a closed dashboard costs no broker calls."""
    last: dict[str, str] = {}
    while True:
        try:
            if hub.subscriber_count:
                # Another producer (e.g. the DB on a proposal write) may already have published
                # this exact state; treat the hub's latest payload as already sent.
                fresh = await snapshots(app.state)
                for ev in hub.latest():
                    last[ev.type] = fingerprint(ev.type, ev.data)
                for type_, data in fresh.items():
                    fp = fingerprint(type_, data)
                    if last.get(type_) != fp:
                        last[type_] = fp
                        hub.publish(type_, data)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the publisher must never die
            log.debug("stream: publisher tick failed", exc_info=True)
        await asyncio.sleep(interval)


async def mux_stream(request: Request, st, heartbeat: float) -> AsyncIterator[str]:
    q = hub.subscribe()
    last: dict[str, str] = {}

    def emit(type_: str, data: Any) -> str | None:
        if type_ not in NO_DIFF:
            fp = fingerprint(type_, data)
            if last.get(type_) == fp:
                return None
            last[type_] = fp
        return sse(type_, data)

    try:
        fresh = await snapshots(st)
        # prime: replay the hub's latest state for types we cannot recompute, then fresh snapshots
        for ev in hub.latest():
            if ev.type not in fresh and (msg := emit(ev.type, ev.data)):
                yield msg
        for type_, data in fresh.items():
            if msg := emit(type_, data):
                yield msg
        sent_at = time.monotonic()
        while not await request.is_disconnected():
            wait = max(0.05, heartbeat - (time.monotonic() - sent_at))
            try:
                ev = await asyncio.wait_for(q.get(), wait)
            except asyncio.TimeoutError:
                yield sse("heartbeat", {"ts": time.time()})
                sent_at = time.monotonic()
                continue
            if msg := emit(ev.type, ev.data):
                yield msg
                sent_at = time.monotonic()
    finally:
        hub.unsubscribe(q)


async def chart_stream(request: Request, feed, wanted: list[str]) -> AsyncIterator[str]:
    """One message per price update (today's behaviour, byte-compatible)."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    for sym in wanted:
        feed.subscribe(sym, queue)
    try:
        hello = {"mode": feed.mode, "last": {s: feed.last.get(s) for s in wanted}}
        yield f"event: hello\ndata: {json.dumps(hello)}\n\n"
        while not await request.is_disconnected():
            try:
                tick = await asyncio.wait_for(queue.get(), CHART_KEEPALIVE)
                yield f"data: {json.dumps(tick)}\n\n"
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        for sym in wanted:
            feed.unsubscribe(sym, queue)


_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.get("/api/stream")
async def stream(request: Request, symbol: str | None = None, symbols: str | None = None):
    st = request.app.state
    if symbol is None and symbols is None:
        return StreamingResponse(mux_stream(request, st, st.settings.stream_heartbeat_seconds),
                                 media_type="text/event-stream", headers=_HEADERS)
    wanted = [chart_symbol(request, x) for x in
              (symbols.split(",") if symbols else [symbol or ""]) if x.strip()]
    if not wanted:
        raise HTTPException(400, "symbol or symbols is required")
    return StreamingResponse(chart_stream(request, st.feed, wanted),
                             media_type="text/event-stream", headers=_HEADERS)
