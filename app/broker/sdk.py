"""Direct Webull OpenAPI SDK transport (``webull-openapi-python-sdk``), order execution only.

STATUS: NOT VERIFIED AGAINST A LIVE ACCOUNT. Validate it on UAT first (WEBULL_ENVIRONMENT=uat
routes it to Webull's sandbox host; see scripts/uat_order_test.py). It is written from the SDK source
(``webull.trade.trade_client.TradeClient.order_v3`` / the way ``webull_openapi_mcp`` builds its
payloads) and has only been exercised with fakes. It is used only when
``BROKER_EXECUTION=sdk`` and ``BROKER_MODE=webull``; the default is the MCP transport.

It FAILS CLOSED: any tool or argument it cannot map exactly raises ``BrokerError`` and nothing is
sent. It never falls back to MCP on its own (that would hide a half-working configuration).
Mapped today: ``place_stock_order``, ``place_stock_combo_order`` (bracket) and ``cancel_order``.
Option orders, replace and crypto are NOT mapped (fail closed).

The SDK's ``ApiClient`` is created once (lazily) and reused, so HTTPS connections are pooled. The
SDK is synchronous, so calls run in a worker thread.

Return values are formatted like the MCP server's order text (``client_order_id: ...``) so the
engine's parsers keep working.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Any

from ..config import ROOT
from .errors import BrokerError

log = logging.getLogger(__name__)

SDK_TOOLS = ("place_stock_order", "place_stock_combo_order", "cancel_order")
_COMBO_LEG_KEYS = {"combo_type", "instrument_type", "market", "symbol", "side", "order_type",
                   "quantity", "limit_price", "stop_price", "time_in_force",
                   "support_trading_session", "client_order_id", "entrust_type"}


def _dec(value: Any) -> str:
    return f"{Decimal(str(value)).normalize():f}"


def _payload(response: Any) -> Any:
    if response is None:
        return None
    js = getattr(response, "json", None)
    if callable(js):
        try:
            return js()
        except Exception:  # noqa: BLE001
            return getattr(response, "text", None)
    return response


def _format(data: Any) -> str:
    if isinstance(data, dict):
        keys = ("client_order_id", "client_combo_order_id", "combo_order_id", "order_id")
        lines = [f"{k}: {data[k]}" for k in keys if k in data]
        if lines:
            return "\n".join(lines)
    return str(data)


class SdkTransport:
    def __init__(self, settings, *, trade_client: Any = None) -> None:
        self.settings = settings
        self._trade = trade_client          # injectable for tests

    # -- client ------------------------------------------------------------
    def _client(self):
        if self._trade is not None:
            return self._trade
        key, secret = os.environ.get("WEBULL_APP_KEY", ""), os.environ.get("WEBULL_APP_SECRET", "")
        if not key or not secret:
            raise BrokerError("SDK execution needs WEBULL_APP_KEY and WEBULL_APP_SECRET")
        try:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"webull-openapi-python-sdk is not importable: {e}") from e
        region = getattr(self.settings, "webull_region", "us")
        environment = str(getattr(self.settings, "webull_environment", "uat")).lower()
        api = ApiClient(key, secret, region)
        # Suppress the SDK's built-in console and file loggers which dump request payloads,
        # signed headers, and credentials on error.
        api._stream_logger_set = True
        api._file_logger_set = True
        logging.getLogger("webull").setLevel(logging.CRITICAL)
        logging.getLogger("webull.core.client").setLevel(logging.CRITICAL)
        api.set_token_dir(os.environ.get("WEBULL_TOKEN_DIR") or str(ROOT / "conf"))
        self._configure_endpoints(api, region, environment)
        try:
            self._trade = TradeClient(api)
        except Exception as e:  # noqa: BLE001 - token / 2FA problems surface here
            text = f"{type(e).__name__}: {e}"
            if "TOKEN" in text.upper() or "NO_AVAILABLE_DEVICE" in text.upper():
                raise BrokerError("Webull login token missing or not approved: run Auth_Trader.bat "
                                  "(or ./auth.sh) and approve the request in the Webull app") from e
            raise BrokerError(f"SDK client init failed: {type(e).__name__}") from e
        return self._trade

    @staticmethod
    def uat_trade_host(region: str) -> str | None:
        """The UAT (sandbox) trading host for ``region``, from the pinned official MCP package
        (the SDK's built-in endpoint table only has production hosts)."""
        try:
            from webull_openapi_mcp.sdk_client import UAT_ENDPOINTS
        except Exception:  # noqa: BLE001
            return None
        return (UAT_ENDPOINTS.get("region_mapping", {}).get(region.lower()) or {}).get("api")

    @classmethod
    def _configure_endpoints(cls, api, region: str, environment: str) -> None:
        """prod: the SDK's built-in endpoints. uat: inject the sandbox trading host (the same one
        the MCP server uses). Anything else fails closed, so an unknown setting never reaches prod."""
        if environment == "prod":
            return
        if environment != "uat":
            raise BrokerError(f"SDK execution: unknown WEBULL_ENVIRONMENT={environment!r} "
                              "(expected uat or prod)")
        host = cls.uat_trade_host(region)
        if not host:
            raise BrokerError(f"SDK execution: no UAT trading endpoint known for region {region!r}")
        from webull.core.common.api_type import DEFAULT
        api.add_endpoint(region.lower(), host, DEFAULT)
        log.info("SDK transport: UAT endpoint %s for region %s", host, region)

    # -- mapping -----------------------------------------------------------
    @staticmethod
    def _stock_order(a: dict) -> dict:
        required = ("symbol", "side", "order_type", "time_in_force", "trading_session",
                    "client_order_id")
        missing = [k for k in required if not a.get(k)]
        if missing:
            raise BrokerError(f"SDK transport: place_stock_order missing {missing}")
        extra = set(a) - set(required) - {"account_id", "quantity", "limit_price", "stop_price",
                                          "entrust_type", "total_cash_amount"}
        if extra:
            raise BrokerError(f"SDK transport: unmapped place_stock_order arguments {sorted(extra)}")
        entrust = a.get("entrust_type", "QTY")
        order = {
            "combo_type": "NORMAL", "client_order_id": a["client_order_id"],
            "instrument_type": "EQUITY", "market": "US", "symbol": a["symbol"],
            "order_type": a["order_type"], "entrust_type": entrust,
            "support_trading_session": a["trading_session"],
            "time_in_force": a["time_in_force"], "side": a["side"],
        }
        if entrust == "AMOUNT":
            if a.get("total_cash_amount") is None:
                raise BrokerError("SDK transport: AMOUNT order without total_cash_amount")
            order["total_cash_amount"] = _dec(a["total_cash_amount"])
        else:
            if a.get("quantity") is None:
                raise BrokerError("SDK transport: order without quantity")
            order["quantity"] = _dec(a["quantity"])
        for k in ("limit_price", "stop_price"):
            if a.get(k) is not None and order["order_type"] != "MARKET":
                order[k] = _dec(a[k])       # a MARKET order carries no price fields
        if order["order_type"] == "LIMIT" and "limit_price" not in order:
            raise BrokerError("SDK transport: LIMIT order without limit_price")
        if order["order_type"] == "STOP_LOSS" and "stop_price" not in order:
            raise BrokerError("SDK transport: STOP_LOSS order without stop_price")
        if order["order_type"] not in ("LIMIT", "STOP_LOSS", "MARKET"):
            raise BrokerError(f"SDK transport: unmapped order_type {order['order_type']!r}")
        return order

    @staticmethod
    def _combo_orders(a: dict) -> tuple[list[dict], str]:
        orders = a.get("orders")
        combo = a.get("client_combo_order_id")
        if not orders or not combo:
            raise BrokerError("SDK transport: combo order needs orders and client_combo_order_id")
        out = []
        for o in orders:
            extra = set(o) - _COMBO_LEG_KEYS
            if extra:
                raise BrokerError(f"SDK transport: unmapped combo leg arguments {sorted(extra)}")
            if not o.get("client_order_id") or not o.get("combo_type"):
                raise BrokerError("SDK transport: combo leg missing client_order_id/combo_type")
            if o.get("instrument_type", "EQUITY") != "EQUITY":
                raise BrokerError("SDK transport: only equity combo legs are mapped")
            # Same shape as webull_openapi_mcp's _build_combo_leg (validated on UAT 2026-11-24):
            # Webull rejects a leg without entrust_type ("invalid entrust_type").
            leg = {
                "combo_type": o["combo_type"], "instrument_type": "EQUITY",
                "market": o.get("market", "US"), "symbol": o["symbol"], "side": o["side"],
                "order_type": o["order_type"], "quantity": _dec(o["quantity"]),
                "entrust_type": "QTY", "time_in_force": o.get("time_in_force", "DAY"),
                "support_trading_session": o.get("support_trading_session", "CORE"),
                "client_order_id": o["client_order_id"],
            }
            for k in ("limit_price", "stop_price"):
                if o.get(k) is not None:
                    leg[k] = _dec(o[k])
            if leg["order_type"] == "LIMIT" and "limit_price" not in leg:
                raise BrokerError("SDK transport: LIMIT combo leg without limit_price")
            if leg["order_type"] == "STOP_LOSS" and "stop_price" not in leg:
                raise BrokerError("SDK transport: STOP_LOSS combo leg without stop_price")
            out.append(leg)
        return out, combo

    # -- execute -----------------------------------------------------------
    async def call(self, name: str, **a) -> str:
        if name not in SDK_TOOLS:
            raise BrokerError(f"SDK transport cannot execute {name!r} (unmapped; failing closed)")
        account_id = a.get("account_id")
        if not account_id:
            raise BrokerError("SDK transport: account_id is required")
        client = self._client()
        if name == "place_stock_order":
            order = self._stock_order(a)
            fn = lambda: client.order_v3.place_order(account_id=account_id, new_orders=[order])  # noqa: E731
        elif name == "place_stock_combo_order":
            orders, combo = self._combo_orders(a)
            fn = lambda: client.order_v3.place_order(  # noqa: E731
                account_id=account_id, new_orders=orders, client_combo_order_id=combo)
        else:
            coid = a.get("client_order_id")
            if not coid:
                raise BrokerError("SDK transport: cancel needs client_order_id")
            fn = lambda: client.order_v3.cancel_order(account_id=account_id, client_order_id=coid)  # noqa: E731
        try:
            response = await asyncio.to_thread(fn)
        except BrokerError:
            raise
        except Exception as e:  # noqa: BLE001 - never leak SDK internals / secrets
            code = getattr(e, "error_code", None)
            if code is not None and getattr(e, "http_status", None):
                # The server answered: the order was refused, not lost (not a transport error).
                raise BrokerError(f"{name} rejected by Webull: HTTP {e.http_status} {code}: "
                                  f"{str(getattr(e, 'error_msg', ''))[:200]}") from e
            raise BrokerError(f"{name} failed via SDK: {type(e).__name__}", transport=True) from e
        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and status >= 400:
            raise BrokerError(f"{name}: SDK HTTP {status}: {str(getattr(response, 'text', ''))[:200]}")
        return _format(_payload(response))
