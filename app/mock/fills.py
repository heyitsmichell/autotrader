"""Order matching, slippage, validation and order text formatting for the mock broker."""

from __future__ import annotations

import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from .options import option_price, option_symbol
from .quotes import price_of
from .state import (ACCOUNT_ID, DISCLAIMER, FRACTIONAL_ELIGIBLE, OPEN_STATUSES, _children, _iso,
                    _now_iso, _set, equity, load, save)


def realism() -> bool:
    """Optional realism mode (MOCK_REALISM=1): spread, partial first fill, per-order fee."""
    return os.environ.get("MOCK_REALISM", "").strip().lower() in ("1", "true", "yes", "on")


def spread_bps() -> float:
    return float(os.environ.get("MOCK_SPREAD_BPS", "5"))


def order_fee() -> float:
    return float(os.environ.get("MOCK_ORDER_FEE", "0.50"))


def _fill(state: dict, order: dict, px: float) -> bool:
    qty, sym = order["quantity"], order["symbol"]
    if order.get("instrument_type") == "OPTION":
        # Option fill
        legs = order.get("legs", [])
        if not legs:
            legs = [order]  # single leg format
        for leg in legs:
            opt_sym = leg["symbol"]
            opt_qty = leg["quantity"]
            pos = state["option_positions"].setdefault(opt_sym, [])
            # Find matching leg or create new
            matched = False
            for p in pos:
                if (p["underlying"] == leg.get("underlying", "") and
                    p["strike"] == leg["strike_price"] and
                    p["expiration"] == leg["option_expire_date"] and
                    p["opt_type"] == leg["option_type"]):
                    if order["side"] == "BUY":
                        if leg.get("position_intent") in ("BUY_TO_OPEN", None):
                            p["qty"] += opt_qty
                        elif leg.get("position_intent") == "BUY_TO_CLOSE":
                            p["qty"] -= opt_qty
                    else:  # SELL
                        if leg.get("position_intent") in ("SELL_TO_OPEN", None):
                            p["qty"] -= opt_qty
                        elif leg.get("position_intent") == "SELL_TO_CLOSE":
                            p["qty"] += opt_qty
                    matched = True
                    if p["qty"] == 0:
                        pos.remove(p)
                    break
            if not matched and order["side"] == "BUY":
                pos.append({
                    "underlying": leg.get("underlying", ""),
                    "strike": leg["strike_price"],
                    "expiration": leg["option_expire_date"],
                    "opt_type": leg["option_type"],
                    "qty": opt_qty,
                    "cost": px,
                })
        order.update(filled_price=px, filled_at=_now_iso())
        _set(order, "FILLED")
        return True
    else:
        # Stock/ETF fill
        pos = state["positions"].setdefault(sym, {"qty": 0.0, "cost": 0.0})
        if order["side"] == "BUY":
            if state["cash"] < qty * px:
                return False
            pos["cost"] = (pos["cost"] * pos["qty"] + px * qty) / (pos["qty"] + qty)
            pos["qty"] += qty
            state["cash"] -= qty * px
        else:
            if pos["qty"] < qty - 1e-9:  # allow tiny floating point
                if pos["qty"] == 0:
                    del state["positions"][sym]
                return False
            pos["qty"] -= qty
            state["cash"] += qty * px
            if pos["qty"] < 1e-9:
                del state["positions"][sym]
        order.update(filled_price=px, filled_at=_now_iso())
        _set(order, "FILLED")
        return True


def try_fill(state: dict, price=price_of) -> None:
    """Expire old DAY orders, fill orders whose price is reached, run bracket logic."""
    today = datetime.now(timezone.utc).date().isoformat()
    changed = True
    partials: set[str] = set()
    while changed:
        changed = False
        for order in state["orders"]:
            if order["status"] not in OPEN_STATUSES:
                continue
            if order.get("tif", "DAY") == "DAY" and order["created_at"][:10] < today:
                _set(order, "CANCELLED")
                for child in _children(state, order):
                    _set(child, "CANCELLED")
                changed = True
                continue
            if order["status"] != "SUBMITTED" or order["client_order_id"] in partials:
                continue
            # Get current price
            if order.get("instrument_type") == "OPTION":
                legs = order.get("legs", [order])
                leg = legs[0]
                px = option_price(leg.get("underlying", ""), leg["strike_price"],
                                  leg["option_expire_date"], leg["option_type"])[0 if order["side"] == "SELL" else 1]
            else:
                px = price(order["symbol"])
            buy = order["side"] == "BUY"
            if order.get("order_type", "LIMIT") == "STOP_LOSS":
                hit = (not buy and px <= order["stop_price"]) or (buy and px >= order["stop_price"])
            elif order.get("order_type") == "MARKET":
                hit = True
            else:
                hit = (buy and order["limit_price"] >= px) or (not buy and order["limit_price"] <= px)
            if not hit:
                continue
            changed = True
            if realism() and order.get("instrument_type") != "OPTION":
                half = spread_bps() / 20000.0           # half-spread as a fraction
                px = round(px * (1 + half) if buy else px * (1 - half), 4)
                total = order["quantity"]
                partial = not order.get("partial_done") and total > 1e-9
                if partial:
                    order["quantity"] = round(total * 0.5, 4) or total
                ok = _fill(state, order, px)
                if ok and not order.get("fee_charged"):
                    state["cash"] -= order_fee()
                    order["fee_charged"] = True
                    order["commission"] = order_fee()
                if ok and partial:
                    done = order["quantity"]
                    order["filled_qty"] = order.get("filled_qty", 0.0) + done
                    order["quantity"] = round(total - done, 4)
                    order["partial_done"] = True
                    order["total_qty"] = total
                    if order["quantity"] > 1e-9:
                        order["status"] = "SUBMITTED"   # remainder stays working
                        partials.add(order["client_order_id"])
                        continue
                if ok and order.get("total_qty"):       # remainder filled: show the full size
                    order["quantity"] = order["filled_qty"] = order["total_qty"]
                if not ok:
                    order["quantity"] = total
            elif not _fill(state, order, px):
                ok = False
            else:
                ok = True
            if not ok:
                _set(order, "FAILED")
                for child in _children(state, order):
                    _set(child, "CANCELLED")
                continue
            for child in _children(state, order):     # MASTER filled: arm the exits
                if child["status"] == "PENDING":
                    _set(child, "SUBMITTED")
            if order.get("oco"):                        # one exit filled: cancel the other
                for o in state["orders"]:
                    if o["client_order_id"] == order["oco"] and o["status"] in OPEN_STATUSES:
                        _set(o, "CANCELLED")


def _detail(o: dict) -> list[str]:
    def v(x):
        return "N/A" if x is None else x
    inst_type = o.get("instrument_type", "EQUITY")
    qty = o["quantity"]
    if isinstance(qty, float):
        qty_str = f"{qty:g}"
    else:
        qty_str = str(qty)
    return [
        f"    Client Order ID:    {o['client_order_id']}",
        f"    Order ID:           {o['order_id']}",
        f"    Symbol:             {o['symbol']}",
        f"    Side:               {o['side']}",
        f"    Status:             {o['status']}",
        f"    Order Type:         {o.get('order_type', 'LIMIT')}",
        f"    Instrument Type:    {inst_type}",
        f"    Total Quantity:     {qty_str}",
        f"    Filled Quantity:    {qty_str if o['status'] == 'FILLED' else 0}",
        f"    Limit Price:        {v(o.get('limit_price'))}",
        f"    Stop Price:         {v(o.get('stop_price'))}",
        f"    Filled Price:       {v(o.get('filled_price'))}",
        f"    Time In Force:      {o.get('tif', 'DAY')}",
        "    Trading Session:    CORE",
        f"    Place Time:         {o['created_at']}",
    ]


def fmt_orders(orders: list[dict], title: str) -> str:
    """Same layout as webull-openapi-mcp's format_open_orders / format_order_history."""
    groups: dict[str, list[dict]] = {}
    for o in orders:
        groups.setdefault(o.get("group_id") or o["client_order_id"], []).append(o)
    if not groups:
        return DISCLAIMER + "No data available."
    lines = [f"=== {title} ==="]
    for i, (gid, legs) in enumerate(groups.items(), 1):
        lines += [f"\n[Order Entry {i}]", f"Client Order ID:  {gid}",
                  f"Combo Type:       {legs[0].get('combo_type', 'NORMAL')}"]
        for j, o in enumerate(legs, 1):
            lines.append(f"\n  --- Order {j} ---" if len(legs) > 1 else "\n  [Order Details]")
            lines += _detail(o)
    return DISCLAIMER + "\n".join(lines)


def check_account(account_id: str) -> str | None:
    return None if account_id == ACCOUNT_ID else f"Account error: unknown account_id {account_id}"


# -- validation helpers --------------------------------------------------------
def _validate_stock(symbol: str, side: str, order_type: str, quantity: float,
                    limit_price: Optional[float], stop_price: Optional[float],
                    entrust_type: str = "QTY", total_cash_amount: Optional[float] = None) -> str | None:
    if side not in ("BUY", "SELL"):
        return "Validation error: side must be BUY or SELL"
    if order_type not in ("LIMIT", "STOP_LOSS", "MARKET"):
        return "Validation error: the mock broker supports LIMIT, MARKET, and STOP_LOSS orders"
    if order_type == "LIMIT" and (not limit_price or limit_price <= 0):
        return "Validation error: limit_price is required for LIMIT orders"
    if order_type == "STOP_LOSS" and (not stop_price or stop_price <= 0):
        return "Validation error: STOP_LOSS order requires 'stop_price' field"
    if quantity <= 0:
        return "Validation error: quantity must be positive"
    # Validate fractional
    is_fractional = quantity != int(quantity) or entrust_type == "AMOUNT"
    if is_fractional:
        if entrust_type != "AMOUNT" and total_cash_amount is None:
            # For fractional QTY, we allow it but warn
            pass
        if symbol.upper() not in FRACTIONAL_ELIGIBLE:
            return f"Validation error: {symbol} is not eligible for fractional trading"
        if entrust_type == "AMOUNT":
            if order_type != "MARKET" and order_type != "LIMIT":
                return "Validation error: fractional (AMOUNT) orders only support MARKET or LIMIT"
            if total_cash_amount is None or total_cash_amount <= 0:
                return "Validation error: total_cash_amount required for AMOUNT entrust_type"
            if total_cash_amount < 1.0:
                return "Validation error: minimum fractional order is $1.00"
    else:
        if quantity != int(quantity):
            return "Validation error: quantity must be a positive whole number"
    return None


def _validate_option(symbol: str, side: str, quantity: int, opt_type: str,
                     strike: float, expiration: str, order_type: str,
                     limit_price: Optional[float], stop_price: Optional[float],
                     position_intent: Optional[str]) -> str | None:
    if side not in ("BUY", "SELL"):
        return "Validation error: side must be BUY or SELL"
    if quantity <= 0 or quantity != int(quantity):
        return "Validation error: option quantity must be a positive whole number (contracts)"
    if opt_type.upper() not in ("CALL", "PUT"):
        return "Validation error: option_type must be CALL or PUT"
    if strike <= 0:
        return "Validation error: strike_price must be positive"
    try:
        datetime.fromisoformat(expiration.replace("Z", "+00:00"))
    except Exception:
        return "Validation error: expiration_date must be valid ISO date (YYYY-MM-DD)"
    if order_type not in ("LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"):
        return "Validation error: option order_type must be LIMIT, MARKET, STOP_LOSS, or STOP_LOSS_LIMIT"
    if order_type in ("LIMIT", "STOP_LOSS_LIMIT") and (not limit_price or limit_price <= 0):
        return "Validation error: limit_price required for LIMIT/STOP_LOSS_LIMIT orders"
    if order_type in ("STOP_LOSS", "STOP_LOSS_LIMIT") and (not stop_price or stop_price <= 0):
        return "Validation error: stop_price required for STOP_LOSS/STOP_LOSS_LIMIT orders"
    if position_intent and position_intent not in ("BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE"):
        return "Validation error: position_intent must be BUY_TO_OPEN, BUY_TO_CLOSE, SELL_TO_OPEN, or SELL_TO_CLOSE"
    return None


def _new_order(state: dict, symbol: str, side: str, order_type: str, quantity: float,
               limit_price, stop_price, tif: str, coid: str, **extra) -> dict:
    order = {"client_order_id": coid, "order_id": f"MOCK{len(state['orders']) + 1:06d}",
             "symbol": symbol.upper(), "side": side, "quantity": quantity,
             "order_type": order_type,
             "limit_price": float(limit_price) if limit_price else None,
             "stop_price": float(stop_price) if stop_price else None,
             "tif": tif, "status": "SUBMITTED", "created_at": _now_iso(),
             "combo_type": "NORMAL", **extra}
    state["orders"].append(order)
    return order
