"""The ``Broker`` facade: composes transport, router, guard and the API mixins."""

from __future__ import annotations

import logging
import os
from typing import Callable

from .crypto import CryptoMixin
from .guard import FlattenMode, OrderGuard
from .market_data import MarketDataMixin
from .options import OptionsMixin
from .orders import OrdersMixin
from .router import OrderRouter
from .transport import McpTransport

log = logging.getLogger(__name__)


def mode_warning_for(settings) -> str | None:
    """L3: mock mode while Webull credentials are present usually means a typo'd/missing .env."""
    if getattr(settings, "broker_mode", "") != "mock":
        return None
    if os.environ.get("WEBULL_APP_KEY") or os.environ.get("WEBULL_APP_SECRET"):
        return ("BROKER_MODE=mock but WEBULL_APP_KEY/WEBULL_APP_SECRET are set: orders are "
                "SIMULATED. Set BROKER_MODE=webull in .env if you intended to trade at Webull.")
    return None


class Broker(MarketDataMixin, OrdersMixin, OptionsMixin, CryptoMixin):
    def __init__(self, settings) -> None:
        self.settings = settings
        self._mcp = McpTransport(settings)
        self._account_id: str | None = settings.webull_account_id
        self._init_market_data()
        self.guard = OrderGuard(settings)
        self.router = OrderRouter(settings, lambda name, **a: self.call(name, **a))
        self.mode_warning: str | None = mode_warning_for(settings)
        if self.mode_warning:
            log.warning(self.mode_warning)

    # -- state delegated to the MCP transport (tests set ``broker.session`` directly) ----------
    @property
    def session(self):
        return self._mcp.session

    @session.setter
    def session(self, value) -> None:
        self._mcp.session = value

    @property
    def tools(self):
        return self._mcp.tools

    @tools.setter
    def tools(self, value) -> None:
        self._mcp.tools = value

    @property
    def error(self):
        return self._mcp.error

    @error.setter
    def error(self, value) -> None:
        self._mcp.error = value

    @property
    def connected(self) -> bool:
        return self._mcp.session is not None

    # -- lifecycle ---------------------------------------------------------
    def _server_params(self):
        return self._mcp._server_params()

    def _mcp_python(self) -> str:
        return self._mcp._mcp_python()

    async def start(self, timeout: float = 90) -> None:
        await self._mcp.start(timeout)

    async def stop(self) -> None:
        await self._mcp.stop()
        self._account_id = self.settings.webull_account_id

    async def call(self, name: str, **arguments) -> str:
        return await self._mcp.call(name, **arguments)

    # -- guard / router ----------------------------------------------------
    def set_guard_context(self, kill_switch: Callable[[], bool] | None = None, db=None) -> None:
        """Called by the engine at init. ``kill_switch()`` returns True when opening orders must
        be refused. ``db`` (optional) receives the L3 mode warning via ``db.log("warn", ...)``."""
        self.guard.set_context(kill_switch=kill_switch)
        if db is not None and self.mode_warning:
            try:
                db.log("warn", self.mode_warning)
            except Exception:  # noqa: BLE001
                log.debug("could not write mode warning to db", exc_info=True)

    def flatten_mode(self) -> FlattenMode:
        """``async with broker.flatten_mode():`` exempts orders from the kill switch."""
        return FlattenMode()

    def _guard(self, order: dict, *, allow_when_killed: bool = False) -> None:
        guard = self.__dict__.get("guard")      # absent on Broker.__new__ test stubs
        if guard is not None:
            guard.check(order, allow_when_killed=allow_when_killed)

    async def _submit(self, tool: str, **args) -> str:
        router = self.__dict__.get("router")
        if router is None:
            return await self.call(tool, **args)
        return await router.submit(tool, **args)
