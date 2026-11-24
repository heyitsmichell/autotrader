"""Event-hub notifications for DB changes (proposals, activity log). Non-blocking.

``DB`` mixins call ``self._proposals_changed()`` / ``self._events_changed()`` after a write. The
publish is coalesced onto the running event loop (one publish per loop turn however many rows
changed) and never raises: a failing hub or a closed database must not break a write.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class _Coalesced:
    def __init__(self, fn) -> None:
        self._fn = fn
        self._pending = False

    def __call__(self) -> None:
        if self._pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._run()           # no loop in this thread: publish directly (hub is thread-safe)
            return
        self._pending = True
        loop.call_soon(self._run)

    def _run(self) -> None:
        self._pending = False
        try:
            self._fn()
        except Exception:  # noqa: BLE001 - e.g. the DB was closed in the meantime
            log.debug("db notification failed", exc_info=True)


class NotifyMixin:
    """Mixin for the DB facade. Needs ``proposals(limit)`` and ``events(limit)``."""

    PROPOSALS_PUBLISH_LIMIT = 50
    EVENTS_PUBLISH_LIMIT = 50

    def _notifier(self, name: str, fn) -> _Coalesced:
        cache = self.__dict__.setdefault("_notifiers", {})
        if name not in cache:
            cache[name] = _Coalesced(fn)
        return cache[name]

    def _proposals_changed(self) -> None:
        def publish() -> None:
            from ..eventhub import hub
            from .schema import decode
            hub.publish("proposals", [decode(r) for r in self.proposals(self.PROPOSALS_PUBLISH_LIMIT)])
        self._notifier("proposals", publish)()

    def _events_changed(self) -> None:
        def publish() -> None:
            from ..eventhub import hub
            hub.publish("events", self.events(self.EVENTS_PUBLISH_LIMIT))
        self._notifier("events", publish)()
