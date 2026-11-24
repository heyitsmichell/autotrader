"""Option chain, quotes and order placement (mixin for ``Broker``)."""

from __future__ import annotations

import time

from ..quotes import Quote
from .errors import BrokerError
from .guard import is_opening_option
from .parsers import parse_option_positions, parse_option_snapshot


class OptionsMixin:
    async def option_contracts(self, underlying_symbols: list[str], *,
                               status: str = "LISTING") -> str:
        """Option chain metadata. This is not a quote or collateral preview."""
        if not underlying_symbols:
            raise BrokerError("At least one option underlying is required")
        return await self.call("get_option_contracts", category="US_OPTION",
                               underlying_symbols=",".join(underlying_symbols), status=status)

    async def option_snapshots(self, option_symbols: list[str]) -> str:
        """Option quotes/Greeks; Webull limits snapshot requests to 20 contracts."""
        symbols = list(dict.fromkeys(s.strip() for s in option_symbols if s.strip()))
        if not symbols or len(symbols) > 20:
            raise BrokerError("Option snapshots require 1 to 20 contract symbols")
        return await self.call("get_option_snapshot", symbols=",".join(symbols),
                               category="US_OPTION")

    async def option_positions(self) -> list[dict]:
        """Get option positions with parsed details."""
        text = await self.call("get_account_positions", account_id=await self.account_id())
        if text.startswith("No data"):
            return []
        return parse_option_positions(text)

    async def parsed_option_snapshots(self, option_symbols: list[str]) -> dict[str, dict]:
        """Parsed option snapshots: bid, ask, last, volume, OI, IV, Greeks and ``as_of``
        (UTC epoch seconds: the venue/mock timestamp if the output has one, else fetch time)."""
        fetched = time.time()
        text = await self.option_snapshots(option_symbols)
        snaps = parse_option_snapshot(text)
        for data in snaps.values():
            if not data.get("as_of"):
                data["as_of"] = fetched
        return snaps

    async def option_quote(self, symbol: str) -> Quote | None:
        """One option contract as a ``Quote`` (bid/ask/last/greeks, ``as_of``), or None."""
        snaps = await self.parsed_option_snapshots([symbol])
        d = snaps.get(symbol.strip().upper())
        if not d:
            return None
        return Quote(symbol=symbol.strip().upper(), bid=d.get("bid"), ask=d.get("ask"),
                     last=d.get("last"), as_of=float(d.get("as_of") or time.time()),
                     source="mock" if self.settings.broker_mode == "mock" else "snapshot",
                     iv=d.get("iv"), delta=d.get("delta"), gamma=d.get("gamma"),
                     theta=d.get("theta"), vega=d.get("vega"), volume=d.get("volume"),
                     open_interest=d.get("open_interest"))

    # -- options order placement -----------------------------------------------
    async def preview_option_order(self, symbol: str, side: str, quantity: int,
                                   option_type: str, strike_price: float,
                                   expiration_date: str, limit_price: float,
                                   order_type: str = "LIMIT", time_in_force: str = "DAY",
                                   position_intent: str = "BUY_TO_OPEN") -> str:
        """Preview a single-leg option order."""
        args = {
            "account_id": await self.account_id(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "option_type": option_type,
            "strike_price": strike_price,
            "expiration_date": expiration_date,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "position_intent": position_intent,
        }
        if order_type in ("LIMIT", "STOP_LOSS_LIMIT"):
            args["limit_price"] = limit_price
        if order_type in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
            args["stop_price"] = limit_price  # Webull uses stop_price for stop orders
        return await self.call("preview_option_order", **args)

    async def place_option_single_order(self, symbol: str, side: str, quantity: int,
                                        option_type: str, strike_price: float,
                                        expiration_date: str, limit_price: float,
                                        client_order_id: str,
                                        order_type: str = "LIMIT", time_in_force: str = "DAY",
                                        position_intent: str = "BUY_TO_OPEN", *,
                                        allow_when_killed: bool = False) -> str:
        """Place a single-leg option order. ``SELL_TO_CLOSE``/``BUY_TO_CLOSE`` are closing and
        bypass the kill switch, caps and whitelist."""
        self._guard({"kind": "option", "symbol": symbol, "side": side, "quantity": quantity,
                     "price": limit_price, "multiplier": 100, "order_type": order_type,
                     "opening": is_opening_option(position_intent, side)},
                    allow_when_killed=allow_when_killed)
        args = {
            "account_id": await self.account_id(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "option_type": option_type,
            "strike_price": strike_price,
            "expiration_date": expiration_date,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "position_intent": position_intent,
            "client_order_id": client_order_id,
        }
        if order_type in ("LIMIT", "STOP_LOSS_LIMIT"):
            args["limit_price"] = limit_price
        if order_type in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
            args["stop_price"] = limit_price
        return await self._submit("place_option_single_order", **args)

    async def place_option_strategy_order(self, legs: list[dict], client_combo_order_id: str,
                                          account_id: str | None = None, *,
                                          allow_when_killed: bool = False) -> str:
        """Place a multi-leg option strategy order."""
        if len(legs) < 2:
            raise BrokerError("Multi-leg strategies require at least 2 legs")
        opening = any(is_opening_option(l.get("position_intent"), l.get("side")) for l in legs)
        first = legs[0]
        qty = max(float(l.get("quantity") or 0) for l in legs)
        price = None
        try:  # net premium when every leg carries a limit price
            net = sum((1 if str(l.get("side", "BUY")).upper() == "BUY" else -1)
                      * float(l["limit_price"]) * float(l.get("quantity") or 0) * 100
                      for l in legs)
            price = abs(net) / qty / 100 if qty else None
        except (KeyError, TypeError, ValueError):
            pass
        self._guard({"kind": "option", "symbol": first.get("symbol", ""), "side": first.get("side"),
                     "quantity": qty, "price": price, "multiplier": 100, "opening": opening},
                    allow_when_killed=allow_when_killed)
        args = {
            "account_id": account_id or await self.account_id(),
            "legs": legs,
            "client_combo_order_id": client_combo_order_id,
        }
        return await self._submit("place_option_strategy_order", **args)

    async def replace_option_order(self, client_order_id: str,
                                   limit_price: float | None = None,
                                   stop_price: float | None = None,
                                   quantity: int | None = None,
                                   time_in_force: str | None = None, *,
                                   allow_when_killed: bool = False) -> str:
        """Replace/modify an existing option order.

        The broker does not know whether the original order was opening or closing, so a replace
        is guarded as an opening order only when it raises the quantity.
        """
        self._guard({"kind": "option", "symbol": "", "side": "BUY", "quantity": quantity or 0,
                     "price": limit_price, "multiplier": 100,
                     "opening": quantity is not None},
                    allow_when_killed=allow_when_killed)
        args = {"account_id": await self.account_id(), "client_order_id": client_order_id}
        if limit_price is not None:
            args["limit_price"] = limit_price
        if stop_price is not None:
            args["stop_price"] = stop_price
        if quantity is not None:
            args["quantity"] = quantity
        if time_in_force is not None:
            args["time_in_force"] = time_in_force
        return await self._submit("replace_option_order", **args)
