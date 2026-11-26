"""Typing-only declarations of the host attributes the Broker mixins rely on.

Each ``*Host`` Protocol lists exactly what that mixin reads or sets on ``self`` but never
defines itself (``Broker.__init__`` / ``core.py`` provide them). Mixins inherit the matching
Protocol under ``TYPE_CHECKING`` only, so there is no runtime effect.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Protocol


class _CallHost(Protocol):
    def call(self, name: str, **arguments: Any) -> Awaitable[str]: ...


class CryptoHost(_CallHost, Protocol):
    pass


class MarketDataHost(_CallHost, Protocol):
    settings: Any
    _account_id: str | None
    _snapshot_cache: dict
    _snapshot_cache_ttl: float
    _snapshot_lock: asyncio.Lock
    _pos_cache: Any
    _pos_gen: int
    _pos_lock: asyncio.Lock
    _bars_shared: Any
    __dict__: dict[str, Any]


class _OrderHost(_CallHost, Protocol):
    def account_id(self) -> Awaitable[str]: ...
    def _guard(self, order: dict, *, allow_when_killed: bool = False) -> None: ...
    def _submit(self, tool: str, **args: Any) -> Awaitable[str]: ...


class OptionsHost(_OrderHost, Protocol):
    settings: Any


class OrdersHost(_OrderHost, Protocol):
    def invalidate_positions(self, *args: Any, **kwargs: Any) -> Any: ...


class BrokerHost(MarketDataHost, OptionsHost, OrdersHost, CryptoHost, Protocol):
    """Everything the Broker mixins need from the composed ``Broker``."""
