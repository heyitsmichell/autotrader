"""Broker-level order guard (defence in depth, M2).

Runs inside every order-placing ``Broker`` method *before* anything is sent. It is independent
of the engine's own risk checks and of the MCP server's env-configured limits.

Rules:
  * Kill switch: refuses OPENING orders. Closing/reducing orders (equity SELL, option
    ``*_TO_CLOSE``, protective stops, cancels) and anything inside ``flatten_mode()`` always pass.
  * Quantity / notional caps (``max_order_qty``, ``max_order_usd`` / ``max_order_notional``;
    options: ``options_max_contracts_per_order`` and notional = price x qty x 100): opening only.
  * Symbol whitelist (``symbol_whitelist``, empty = no restriction): opening only. A position that
    is already held outside the whitelist can still be closed.

Order dict (all keys optional except what the rule needs)::

    {"kind": "stock"|"option"|"crypto", "symbol": str, "side": "BUY"|"SELL",
     "quantity": float, "price": float|None, "amount": float|None,   # amount = dollar-amount order
     "opening": bool, "multiplier": 100 for options,
     "order_type": "MARKET" -> ``price`` is the reference (last) price; notional = price x qty x
     (1 + market_notional_buffer_pct/100). Closing MARKET orders (exits) always pass.}
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

from .errors import OrderGuardError

log = logging.getLogger(__name__)

_flatten: contextvars.ContextVar[bool] = contextvars.ContextVar("broker_flatten_mode", default=False)


class FlattenMode:
    """``with``/``async with`` context that exempts orders from the kill switch.

    Task-local (a ContextVar), so a flatten running in one task does not open the gate for
    entries placed concurrently by another task.
    """

    def __init__(self) -> None:
        self._tokens: list = []

    def __enter__(self):
        self._tokens.append(_flatten.set(True))
        return self

    def __exit__(self, *exc) -> None:
        _flatten.reset(self._tokens.pop())

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc) -> None:
        self.__exit__(*exc)


def in_flatten_mode() -> bool:
    return _flatten.get()


def is_opening_option(position_intent: str | None, side: str | None = None) -> bool:
    intent = (position_intent or "").upper()
    if intent:
        return not intent.endswith("_TO_CLOSE")
    return str(side or "BUY").upper() == "BUY"


class OrderGuard:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.kill_switch: Callable[[], bool] | None = None

    def set_context(self, kill_switch: Callable[[], bool] | None = None) -> None:
        if kill_switch is not None:
            self.kill_switch = kill_switch

    def _killed(self) -> bool:
        if self.kill_switch is None:
            return False
        try:
            return bool(self.kill_switch())
        except Exception:  # noqa: BLE001 - an unreadable kill switch must fail closed for entries
            log.exception("kill_switch callable raised; treating as ON for opening orders")
            return True

    def check(self, order: dict, *, allow_when_killed: bool = False) -> None:
        """Raise OrderGuardError when the order must not be sent. Closing orders always pass."""
        if not order.get("opening", True):
            return
        s = self.settings
        symbol = str(order.get("symbol") or "").upper()
        kind = order.get("kind", "stock")

        if self._killed() and not (allow_when_killed or in_flatten_mode()):
            raise OrderGuardError(f"Kill switch is ON: opening order for {symbol} refused")

        whitelist = {w.upper() for w in (getattr(s, "symbol_whitelist", None) or [])}
        if whitelist and symbol and symbol not in whitelist:
            raise OrderGuardError(f"{symbol} is not in the symbol whitelist; order refused")

        qty = float(order.get("quantity") or 0)
        mult = float(order.get("multiplier") or (100 if kind == "option" else 1))
        if kind == "option":
            max_contracts = int(getattr(s, "options_max_contracts_per_order", 0) or 0)
            if max_contracts and qty > max_contracts:
                raise OrderGuardError(
                    f"{symbol}: {qty:g} contracts exceeds the per-order limit of {max_contracts}")
        elif order.get("amount") is None:
            max_qty = float(getattr(s, "max_order_qty", 0) or 0)
            if max_qty and qty > max_qty:
                raise OrderGuardError(f"{symbol}: quantity {qty:g} exceeds max_order_qty {max_qty:g}")

        notional = order.get("amount")
        if notional is None:
            price = order.get("price")
            if str(order.get("order_type") or "").upper() == "MARKET":
                # A MARKET order has no limit price: size it from the reference (last) price plus
                # a buffer for the move before it fills. No reference price -> refuse (fail closed).
                if price is None or float(price) <= 0:
                    raise OrderGuardError(f"{symbol}: MARKET opening order has no reference price")
                buf = float(getattr(s, "market_notional_buffer_pct", 1.0) or 0.0)
                price = float(price) * (1 + buf / 100.0)
            notional = None if price is None else float(price) * qty * mult
        if notional is not None:
            caps = [float(c) for c in (getattr(s, "max_order_usd", 0),
                                       getattr(s, "max_order_notional", 0)) if c]
            cap = min(caps) if caps else 0.0
            if cap and notional > cap + 1e-9:
                raise OrderGuardError(
                    f"{symbol}: notional ${notional:,.2f} exceeds the order cap ${cap:,.2f}")
