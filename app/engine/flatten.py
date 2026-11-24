"""Emergency actions."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

from .. import consensus, exits, ta
from ..broker import Broker, BrokerError
from ..config import Settings, estimate_cost
from ..core import (calculate_core_state, create_rebalance_plan, check_satellite_drawdown_guard,
                    format_core_satellite_report, satellite_headroom_usd)
from ..db import DB, DuplicateProposalError, decode
from ..eligibility import check_eligibility
from ..eventhub import hub
from ..order_style import cancel_reply_confirmed, exit_should_be_market, market_rejection_is_definitive
from ..market import (market_is_open, market_is_open_extended, now_et, now_utc, order_session,
                      session_phase)
from ..portfolio import ACTIVE, DONE_CANCELLED, parse_orders
from ..risk import OptionRiskManager, RiskManager
from ..sizing import max_allowed_qty, ml_size_factor, volatility_scaled_qty
from .util import (MAX_PROPOSALS_PER_RUN, _coid, _is_option_symbol, _session_rejection,
                   normalize_proposal)

log = logging.getLogger("app.engine")


class FlattenMixin:
    async def cancel_open_orders(self, include_protective: bool, why: str) -> int:
        """Cancel working orders at the broker. With include_protective=False only
        entries (BUYs) are cancelled, and every stop and target stays in place.
        Raises BrokerError if the open orders can't be read."""
        n = 0
        unconfirmed: set[str] = set()       # cancels with no confirmation: the order may be live
        for o in parse_orders(await self.broker.open_orders_text()):
            st = (o.get("status") or "").upper()
            if not (st in ACTIVE or st.startswith(("PENDING", "PARTIAL"))):
                continue
            if not include_protective and (o.get("side") or "").upper() != "BUY":
                continue
            try:
                reply = await self.broker.cancel_order(o["client_order_id"])
                if not cancel_reply_confirmed(reply):
                    self.db.log("warn", f"Cancel of {o.get('symbol')} ({o['client_order_id']}) "
                                        f"not confirmed: {str(reply)[:80]}")
                    unconfirmed.add(o["client_order_id"])
                    continue
                n += 1
            except BrokerError as e:
                unconfirmed.add(o["client_order_id"])
                if e.transport:
                    self.db.log("error", f"Cancel of {o.get('side')} {o.get('symbol')} "
                                         f"({o['client_order_id']}) got no reply: {e}")
        if include_protective:
            # a row whose cancel was not confirmed stays tracked, so reconcile neither stacks a
            # second stop on a possibly-live one nor loses track of it (_sync settles it later)
            for x in self.db.protective(open_only=True):
                if x["client_order_id"] in unconfirmed:
                    continue
                self.db.set_protective(x["id"], "cancelled", why)
        if n:
            self.db.log("warn", f"Cancelled {n} open order(s) at the broker ({why})", alert=True)
        return n

    async def flatten(self, actor: str, reason: str) -> dict:
        """Kill switch on, cancel every open order, then sell every stock position with
        marketable DAY limit orders (last price minus FLATTEN_SLIPPAGE_PCT), split to fit
        the per-order $ and share limits. Positions are unprotected only for the moment
        between the cancels and the sells."""
        if self.flattening:
            raise ValueError("A flatten is already running")
        await self.set_kill_switch(True, f"flatten: {reason}")
        if order_session() is None:
            self.db.log("error", "Flatten requested while every trading session is closed: the "
                                 "kill switch is on, nothing was sold, and stops at Webull stay "
                                 "in place.", alert=True)
            raise ValueError("No trading session is open. The kill switch is on, but positions "
                             "can only be sold during a trading session; stops at Webull stay "
                             "in place.")
        self.flattening = True
        result: dict = {"cancelled": 0, "orders": [], "errors": []}
        try:
            # flatten_mode exempts these orders from the broker guard's kill-switch check (the
            # kill switch was just turned on above, and flatten is the one thing it must allow).
            async with self.broker.flatten_mode(), self._exec_lock:
                try:
                    result["cancelled"] = await self.cancel_open_orders(True, f"flatten: {reason}")
                    await asyncio.sleep(1)          # let the cancels settle at the broker
                    held = await self.broker.positions()
                except BrokerError as e:
                    msg = f"Flatten stopped: could not read orders or positions: {e}"
                    self.db.log("error", msg)
                    result["errors"].append(msg)
                    return result
                for sym, qty in sorted(held.items()):
                    await self._flatten_symbol(sym, qty, actor, reason, result)
                await self._flatten_options(actor, reason, result)
            placed = sum(1 for o in result["orders"] if o["status"] == "placed")
            self.db.log("warn", f"FLATTEN done ({reason}): {result['cancelled']} order(s) "
                                f"cancelled, {placed} sell order(s) placed"
                        + (f", {len(result['errors'])} problem(s): " + "; ".join(result["errors"])
                           if result["errors"] else ""), alert=True)
            return result
        finally:
            self.flattening = False
            if self.on_order:
                self.on_order()

    async def _flatten_symbol(self, sym: str, qty: float, actor: str, reason: str,
                              result: dict) -> None:
        # Handle fractional shares when fractional trading is enabled
        is_frac = self.s.fractional_enabled and qty != int(qty)
        whole = int(qty)
        fractional_part = qty - whole

        try:
            last = await self.broker.last_price(sym)
        except BrokerError as e:
            last = None
            self.db.log("error", f"Flatten: no price for {sym}: {e}")
        if not last:
            result["errors"].append(f"no price for {sym}; sell it in Webull")
            return
        limit = round(last * (1 - self.s.flatten_slippage_pct / 100), 2)

        # Sell whole shares first
        if whole > 0:
            chunk = min(self.s.max_order_qty, int(self.s.max_order_usd // limit))
            if chunk < 1:
                result["errors"].append(f"one {sym} share (${limit:,.2f}) is over MAX_ORDER_USD; "
                                        "sell it in Webull")
                return
            left = whole
            while left > 0:
                n = min(chunk, left)
                status = await self._flatten_sell(sym, n, limit, actor, reason)
                result["orders"].append({"symbol": sym, "quantity": n, "limit_price": limit,
                                         "status": status})
                if status != "placed":
                    result["errors"].append(f"{sym} sell {status}; {left} share(s) not sold")
                    return
                left -= n

        # Sell fractional remainder if fractional trading is enabled
        if is_frac and fractional_part > 1e-9:
            # For fractional, use AMOUNT (dollar-based) order
            notional = fractional_part * limit
            if notional >= self.s.fractional_min_usd:
                status = await self._flatten_sell_fractional(sym, fractional_part, limit, notional, actor, reason)
                result["orders"].append({"symbol": sym, "quantity": fractional_part, "limit_price": limit,
                                         "status": status, "notional": notional})
                if status != "placed":
                    result["errors"].append(f"{sym} fractional sell {status}; {fractional_part:g} share(s) not sold")
            else:
                result["errors"].append(f"{sym} fractional remainder {fractional_part:g} shares "
                                        f"(${notional:.2f}) below FRACTIONAL_MIN_USD (${self.s.fractional_min_usd:.2f}); "
                                        "sell manually in Webull")

    async def _flatten_sell_fractional(self, sym: str, qty: float, limit: float, notional: float,
                                       actor: str, reason: str) -> str:
        """Sell fractional shares using dollar-based (AMOUNT) order."""
        coid = _coid()
        p = {"symbol": sym, "side": "SELL", "quantity": qty, "limit_price": limit,
             "confidence": None, "rationale": f"Flatten: {reason}",
             "entrust_type": "AMOUNT", "total_cash_amount": notional}
        # the coid goes into the row before the broker sees it
        pid = self._add_exit_row(p, "submitting", "flatten", limit, client_order_id=coid,
                                 decided_by=actor, placed_at=now_utc().isoformat(timespec="seconds"))
        trading_session = order_session() or "AUTO"
        try:
            # Use place_order with entrust_type=AMOUNT and total_cash_amount
            response = await self.broker.place_order(sym, "SELL", qty, limit, coid,
                                                     entrust_type="AMOUNT", total_cash_amount=notional,
                                                     trading_session=trading_session,
                                                     allow_when_killed=True)
        except BrokerError as e:
            status = "unknown" if e.transport else "failed"
            if pid is not None:
                self.db.update_proposal(pid, status=status, broker_response=str(e),
                                        reason=f"flatten fractional sell: {e}")
            self.db.log("error", f"Flatten fractional sell of {qty:g} {sym} {status}: {e}")
            return status
        if pid is not None:
            self.db.update_proposal(pid, status="placed", broker_response=response)
        self.order_failures = 0
        self.db.log("info", f"Flatten fractional sell of {qty:g} {sym} @ ${limit:.2f} placed ({actor}).")
        return "placed"

    async def _flatten_sell(self, sym: str, qty: int, limit: float, actor: str,
                            reason: str) -> str:
        coid = _coid()
        p = {"symbol": sym, "side": "SELL", "quantity": qty, "limit_price": limit,
             "confidence": None, "rationale": f"Flatten: {reason}"}
        # the coid goes into the row before the broker sees it; a duplicate open row never
        # blocks the sell (it is settled out of the way, see _add_exit_row)
        pid = self._add_exit_row(p, "submitting", "flatten", limit, client_order_id=coid,
                                 decided_by=actor, placed_at=now_utc().isoformat(timespec="seconds"))
        trading_session = order_session() or "AUTO"
        # EXIT_ORDER_TYPE=market: a flatten sells at the market in the CORE session; extended
        # hours (and a rejected MARKET) keep the marketable limit.
        order_type = "MARKET" if exit_should_be_market(self.s, trading_session) else "LIMIT"
        try:
            try:
                if pid is not None:
                    self.db.update_proposal(pid, order_type=order_type.lower())
                response = await self.broker.place_order(sym, "SELL", qty, limit, coid,
                                                         trading_session=trading_session,
                                                         allow_when_killed=True,
                                                         order_type=order_type)
            except BrokerError as e0:
                if order_type != "MARKET" or e0.transport:
                    raise
                if not market_rejection_is_definitive(e0):
                    # Ambiguous (HTTP 5xx / internal error): the MARKET may be live. No second
                    # SELL; the row goes 'unknown' and reconcile settles it by client_order_id.
                    raise BrokerError(str(e0), transport=True) from e0
                self.db.log("warn", f"Flatten: MARKET sell of {qty} {sym} rejected ({e0}); "
                                    "re-sending as a marketable limit")
                coid = _coid()
                if pid is not None:
                    self.db.update_proposal(pid, client_order_id=coid, order_type="limit")
                response = await self.broker.place_order(sym, "SELL", qty, limit, coid,
                                                         trading_session=trading_session,
                                                         allow_when_killed=True)
        except BrokerError as e:
            status = "unknown" if e.transport else "failed"
            if pid is not None:
                self.db.update_proposal(pid, status=status, broker_response=str(e),
                                        reason=f"flatten sell: {e}")
            self.db.log("error", f"Flatten sell of {qty} {sym} {status}: {e}")
            return status
        if pid is not None:
            self.db.update_proposal(pid, status="placed", broker_response=response)
        return "placed"

    def _add_exit_row(self, p: dict, status: str, reason: str, ref_price: float,
                      **extra) -> int | None:
        """Insert the row for an exit order. Exits are never blocked by the one-open-proposal
        index (``DuplicateProposalError``): the row that holds the slot is settled out of the way
        (pending -> dismissed; submitting -> unknown, to be settled by its client_order_id) and the
        insert is retried. Returns None only if the row still cannot be created, in which case
        the caller sends the order anyway."""
        for _ in range(2):
            try:
                return self.db.add_proposal(None, p, status, reason, ref_price, None, **extra)
            except DuplicateProposalError:
                for row in self.db.open_proposals(p["symbol"], p["side"]):
                    try:
                        if row["status"] == "pending":
                            self.db.update_proposal(row["id"], status="dismissed",
                                                    reason="superseded by an exit order")
                        else:
                            self.db.update_proposal(
                                row["id"], status="unknown",
                                reason="superseded by an exit order; settle by client_order_id")
                    except DuplicateProposalError:
                        pass
        self.db.log("error", f"Could not record the exit row for {p['symbol']}; sending the order "
                             "without one (its client_order_id is in the broker's order list).")
        return None

    async def _flatten_options(self, actor: str, reason: str, result: dict) -> None:
        """Close every held option at a marketable limit as part of a flatten."""
        try:
            closed = await self.option_exits.close_all(f"flatten: {reason}", actor=actor)
        except Exception as e:  # noqa: BLE001 - options must not stop the equity flatten result
            msg = f"Flatten: option close-out failed: {e}"
            self.db.log("error", msg)
            result["errors"].append(msg)
            return
        for c in closed:
            result["orders"].append({"symbol": c["symbol"], "quantity": c["quantity"],
                                     "limit_price": c["limit_price"], "status": c["status"]})
            if c["status"] != "placed":
                result["errors"].append(f"{c['symbol']} option close {c['status']}")
