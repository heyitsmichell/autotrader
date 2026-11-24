"""Equity order placement (mixin for ``Broker``).

Every order method:
  * accepts a caller-supplied client order id and sends it unchanged (never regenerated, also not
    on a retry inside the transport);
  * runs ``Broker._guard`` before anything is sent;
  * sends through ``Broker._submit`` (OrderRouter) rather than calling the transport directly.
"""

from __future__ import annotations

from .. import market
from .errors import BrokerError

_SESSION_ALIASES = {"EXTENDED": "ALL", "ALL": "ALL", "NIGHT": "NIGHT", "CORE": "CORE", "AUTO": "AUTO"}


def resolve_session(requested: str, *, entrust_type: str = "QTY", at=None,
                    queue_for_core: bool = False) -> str:
    """Validate a requested trading session against the clock and return the value
    to send to Webull. Raises BrokerError (non-transport) when it would be rejected.

    NIGHT is Webull's overnight session (20:00-04:00 ET), NOT premarket/after-hours;
    those are ALL. Fractional/AMOUNT orders are CORE-only at Webull.

    queue_for_core=True (protective stops only) lets a CORE order through while another
    session is open: Webull rests a GTC CORE order and activates it at the open. A fully
    closed market (weekend/holiday) is still refused.
    """
    req = _SESSION_ALIASES.get(str(requested).upper())
    if req is None:
        raise BrokerError(f"Unknown trading session {requested!r}")
    current = market.order_session(at)
    if current is None:
        raise BrokerError("No trading session is open (weekend/holiday); order not sent")
    if req == "AUTO":
        req = current
    if req == "ALL" and current == "CORE":
        req = "CORE"                      # inside regular hours CORE is always valid
    if queue_for_core and req == "CORE":
        return "CORE"
    if req != current and not (req == "ALL" and current == "ALL"):
        raise BrokerError(f"Session {req} is not open now (open session: {current}); "
                          "order not sent")
    if entrust_type == "AMOUNT" and req != "CORE":
        raise BrokerError("Fractional/dollar-amount orders are only accepted in the "
                          "regular session; order not sent")
    return req


def _check_order_type(order_type: str, session: str) -> str:
    """LIMIT or MARKET. MARKET only in the CORE session (never sent otherwise)."""
    ot = str(order_type).upper()
    if ot not in ("LIMIT", "MARKET"):
        raise BrokerError(f"Unsupported order type {order_type!r} for place_order; order not sent")
    if ot == "MARKET" and session != "CORE":
        raise BrokerError(f"MARKET orders are only accepted in the CORE session (not {session}): "
                          "Webull rejects market orders with extended hours; order not sent")
    return ot


class OrdersMixin:
    async def preview_order(self, symbol: str, side: str, quantity: int, limit_price: float,
                            entrust_type: str = "QTY", total_cash_amount: float | None = None,
                            trading_session: str = "AUTO", order_type: str = "LIMIT") -> str:
        mapped_session = resolve_session(trading_session, entrust_type=entrust_type)
        order_type = _check_order_type(order_type, mapped_session)
        args = {
            "account_id": await self.account_id(), "symbol": symbol,
            "side": side, "order_type": order_type, "time_in_force": "DAY",
            "quantity": quantity, "trading_session": mapped_session,
        }
        if order_type == "LIMIT":
            args["limit_price"] = limit_price
        if entrust_type == "AMOUNT" and total_cash_amount is not None:
            args["entrust_type"] = "AMOUNT"
            args["total_cash_amount"] = total_cash_amount
        return await self.call("preview_stock_order", **args)

    async def place_order(self, symbol: str, side: str, quantity: float, limit_price: float,
                          client_order_id: str, entrust_type: str = "QTY",
                          total_cash_amount: float | None = None,
                          trading_session: str = "AUTO", *,
                          allow_when_killed: bool = False, order_type: str = "LIMIT") -> str:
        """Place a stock order.

        Args:
            trading_session: "CORE" regular hours (9:30-16:00 ET); "ALL" premarket +
                regular + after-hours (4:00-20:00 ET); "NIGHT" the overnight session
                only (20:00-04:00 ET). "EXTENDED" is accepted as an alias for "ALL".
                "AUTO" (the default) picks the session for the current time.
            client_order_id: caller-supplied; sent as is and reused on any retry.
            order_type: "LIMIT" (default) or "MARKET". MARKET is refused here (BrokerError, nothing
                sent) outside the CORE session: Webull rejects market-type orders with extended
                hours (OPENAPI_GTX_NOT_SUPPORT_MARKET). For MARKET, ``limit_price`` is only the
                reference price for the order guard and is NOT sent to Webull.

        Raises BrokerError (not a transport error) before anything is sent if the
        session is not open right now, so a mismatch can never reach the broker.
        Raises OrderGuardError if the order guard refuses it.
        """
        order_type = str(order_type).upper()
        self._guard({"kind": "stock", "symbol": symbol, "side": side, "quantity": quantity,
                     "price": limit_price, "order_type": order_type,
                     "amount": total_cash_amount if entrust_type == "AMOUNT" else None,
                     "opening": str(side).upper() == "BUY"},
                    allow_when_killed=allow_when_killed)
        mapped_session = resolve_session(trading_session, entrust_type=entrust_type)
        order_type = _check_order_type(order_type, mapped_session)

        args = {
            "account_id": await self.account_id(), "symbol": symbol,
            "side": side, "order_type": order_type, "time_in_force": "DAY",
            "quantity": quantity,
            "trading_session": mapped_session, "client_order_id": client_order_id,
        }
        if order_type == "LIMIT":
            args["limit_price"] = limit_price
        if entrust_type == "AMOUNT" and total_cash_amount is not None:
            args["entrust_type"] = "AMOUNT"
            args["total_cash_amount"] = total_cash_amount
        return await self._submit("place_stock_order", **args)

    async def place_bracket(self, symbol: str, quantity: int, limit_price: float,
                            take_profit: float, stop_loss: float, coids: dict[str, str],
                            combo_id: str, trading_session: str = "AUTO", *,
                            allow_when_killed: bool = False) -> str:
        """BUY entry plus a take-profit and a stop-loss that Webull activates when the
        entry fills (MASTER triggers STOP_PROFIT and STOP_LOSS; filling one cancels the
        other). The exit legs are GTC, so they keep protecting the position after today
        and while this app is off.

        The session is validated against the clock. Webull accepts the same session on
        each combo leg, so extended-hours and overnight entries can keep their exits.
        """
        self._guard({"kind": "stock", "symbol": symbol, "side": "BUY", "quantity": quantity,
                     "price": limit_price, "opening": True}, allow_when_killed=allow_when_killed)
        session = resolve_session(trading_session)
        leg = {"symbol": symbol, "quantity": quantity, "support_trading_session": session,
               "market": "US", "instrument_type": "EQUITY"}
        orders = [
            {**leg, "combo_type": "MASTER", "side": "BUY", "order_type": "LIMIT",
             "limit_price": limit_price, "time_in_force": "DAY", "client_order_id": coids["entry"]},
            {**leg, "combo_type": "STOP_PROFIT", "side": "SELL", "order_type": "LIMIT",
             "limit_price": take_profit, "time_in_force": "GTC", "client_order_id": coids["tp"]},
            {**leg, "combo_type": "STOP_LOSS", "side": "SELL", "order_type": "STOP_LOSS",
             "stop_price": stop_loss, "time_in_force": "GTC", "client_order_id": coids["sl"]},
        ]
        return await self._submit("place_stock_combo_order", account_id=await self.account_id(),
                                  orders=orders, client_combo_order_id=combo_id)

    async def place_stop(self, symbol: str, quantity: float, stop_price: float,
                         client_order_id: str, trading_session: str = "CORE", *,
                         allow_when_killed: bool = False) -> str:
        """A standalone GTC stop-loss sell (used to re-protect shares after a partial exit).

        Webull rejects STOP (stop-market) orders that include extended hours
        (OPENAPI_GTX_NOT_SUPPORT_MARKET), so the stop always goes to the CORE session: outside
        regular hours it rests and becomes active at the open. Any other requested session is
        tried as asked and, on that error, retried once as CORE."""
        self._guard({"kind": "stock", "symbol": symbol, "side": "SELL", "quantity": quantity,
                     "price": stop_price, "opening": False}, allow_when_killed=allow_when_killed)
        requested = str(trading_session).upper()
        session = resolve_session("CORE" if requested == "AUTO" else requested,
                                  queue_for_core=True)

        async def send(sess: str) -> str:
            return await self._submit(
                "place_stock_order", account_id=await self.account_id(), symbol=symbol,
                side="SELL", order_type="STOP_LOSS", time_in_force="GTC", quantity=quantity,
                stop_price=stop_price, trading_session=sess, client_order_id=client_order_id)

        try:
            return await send(session)
        except BrokerError as e:
            if "OPENAPI_GTX_NOT_SUPPORT_MARKET" in str(e) and session != "CORE":
                return await send("CORE")
            raise

    async def cancel_order(self, client_order_id: str) -> str:
        # Cancels reduce risk: not guarded, and never negative-cached (see transport).
        return await self._submit("cancel_order", account_id=await self.account_id(),
                                  client_order_id=client_order_id)
