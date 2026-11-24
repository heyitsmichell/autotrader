"""In-process event hub: one publisher API for the engine, one subscriber API for SSE.

Producers (engine, broker, feed, supervisor) call ``hub.publish(type, payload)`` from any thread
or coroutine. Consumers (the ``/api/stream`` SSE route) call ``hub.subscribe()`` to get an
``asyncio.Queue`` of ``Event`` objects and ``hub.unsubscribe(q)`` when the client disconnects.

Event types (keep in sync with web/src/lib/stream.ts):
    status, portfolio, proposals, events, quote, fill, kill, heartbeat

Publishing never blocks and never raises: a slow consumer whose queue is full drops its oldest
event (the dashboard re-syncs from the next snapshot). The hub keeps the latest payload per
type so a new subscriber can be primed immediately (``hub.latest()``).
"""

from __future__ import annotations

import asyncio
import logging
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

EVENT_TYPES = ("status", "portfolio", "proposals", "events", "quote", "fill", "kill", "heartbeat")
# Types whose latest value is replayed to a new subscriber (quotes/fills are a stream, not state).
SNAPSHOT_TYPES = ("status", "portfolio", "proposals", "events", "kill")


def _fingerprint(data: Any) -> str | None:
    try:
        return json.dumps(data, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - unserialisable payloads are never deduplicated
        return None


@dataclass(frozen=True)
class Event:
    type: str
    data: Any
    ts: float = field(default_factory=time.time)


class EventHub:
    def __init__(self, maxsize: int = 500) -> None:
        self._maxsize = maxsize
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._latest: dict[str, Event] = {}
        self._fps: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.append((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(lp, x) for lp, x in self._subs if x is not q]

    def latest(self) -> list[Event]:
        with self._lock:
            return [self._latest[t] for t in SNAPSHOT_TYPES if t in self._latest]

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, type_: str, data: Any) -> None:
        if type_ not in EVENT_TYPES:
            log.debug("eventhub: unknown event type %s", type_)
        ev = Event(type_, data)
        with self._lock:
            if type_ in SNAPSHOT_TYPES:
                # Several producers publish the same state (the DB on a write, the API's
                # snapshot publisher); re-publishing an identical snapshot is a no-op.
                fp = _fingerprint(data)
                if fp is not None and self._fps.get(type_) == fp and type_ in self._latest:
                    return
                self._latest[type_] = ev
                self._fps[type_] = fp
            subs = list(self._subs)
        for loop, q in subs:
            try:
                if loop.is_closed():
                    continue
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is loop:
                    self._put(q, ev)
                else:
                    loop.call_soon_threadsafe(self._put, q, ev)
            except Exception:  # noqa: BLE001 - publishing must never break a producer
                log.debug("eventhub: publish to a subscriber failed", exc_info=True)

    @staticmethod
    def _put(q: asyncio.Queue, ev: Event) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass


hub = EventHub()
