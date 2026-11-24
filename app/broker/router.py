"""OrderRouter: picks the transport that executes MUTATING order tools.

    BROKER_MODE=mock                      -> "mock"  (MCP transport talking to the mock server)
    BROKER_MODE=webull, BROKER_EXECUTION=mcp (default) -> "mcp"
    BROKER_MODE=webull, BROKER_EXECUTION=sdk            -> "sdk"  (SdkTransport; unverified)

Reads (quotes, positions, order history) always go through MCP. Preview tools are not mutating, so
they also stay on MCP. In "sdk" mode an order tool the SDK transport cannot map raises BrokerError;
it is never silently re-routed to MCP.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .errors import BrokerError
from .sdk import SDK_TOOLS, SdkTransport
from .transport import is_mutating_tool

log = logging.getLogger(__name__)


def select_execution(settings) -> str:
    if getattr(settings, "broker_mode", "mock") == "mock":
        return "mock"           # mock mode ignores BROKER_EXECUTION
    return "sdk" if str(getattr(settings, "broker_execution", "mcp")).lower() == "sdk" else "mcp"


class OrderRouter:
    def __init__(self, settings, mcp_call: Callable[..., Awaitable[str]],
                 sdk: SdkTransport | None = None) -> None:
        self.settings = settings
        self.mode = select_execution(settings)
        self._mcp_call = mcp_call
        self._sdk = sdk
        if self.mode == "sdk":
            self._sdk = self._sdk or SdkTransport(settings)
            log.warning("BROKER_EXECUTION=sdk: orders go through the Webull SDK transport, which is "
                        "NOT verified against a live account. Unmapped order types fail closed.")

    @property
    def transport_name(self) -> str:
        return {"mock": "McpTransport(mock)", "mcp": "McpTransport", "sdk": "SdkTransport"}[self.mode]

    async def submit(self, tool: str, **args) -> str:
        """Execute a mutating order tool. ``args`` (incl. client_order_id) are passed untouched."""
        if not is_mutating_tool(tool):
            raise BrokerError(f"OrderRouter only routes mutating tools, not {tool!r}")
        if self.mode == "sdk":
            if tool not in SDK_TOOLS:
                raise BrokerError(f"{tool}: not supported by BROKER_EXECUTION=sdk; "
                                  "order not sent (use BROKER_EXECUTION=mcp)")
            return await self._sdk.call(tool, **args)
        return await self._mcp_call(tool, **args)
