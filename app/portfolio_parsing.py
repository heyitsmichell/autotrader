"""Pure parsers and constants for the Webull MCP text output (balance, positions, orders)."""

from __future__ import annotations

import re
from datetime import datetime

from .broker import _num
from .market import now_utc

log = __import__("logging").getLogger(__name__)

BALANCE_FIELDS = {
    "currency": "Currency", "cash": "Total Cash Balance", "market_value": "Total Market Value",
    "unrealized": "Total Unrealized P&L", "net_liq": "Net Liquidation", "day_pnl": "Day P&L",
    "buying_power": "Buying Power",
}
ACTIVE = {"SUBMITTED", "PARTIAL_FILLED", "PARTIALLY_FILLED", "WORKING", "NEW", "ACCEPTED"}
DONE_CANCELLED = {"CANCELLED", "CANCELED", "FAILED", "REJECTED", "EXPIRED"}
STALE_UNKNOWN_SECONDS = 300.0       # an 'unknown' stop row this old must show up at the broker
EXIT_RETRY_SECONDS = 900.0          # one exit order per symbol per window
REANCHOR_RETRY_SECONDS = 300.0      # stale-stop cancel attempts per trade
# cancel replies that are NOT a confirmation (the MCP returns them as plain text, not an error)
UNCONFIRMED_CANCEL = ("not found", "may have been filled", "permission denied")


def balance_is_empty(b: dict | None) -> bool:
    """True for a balance the broker could not really have meant: every money field missing
    or zero. A real account never reports zero net liquidation, zero cash and zero buying
    power at once, but the broker does answer that way while its connection is being
    re-established. Storing it wrecks the equity curve, the P&L and the drawdown peak."""
    if not b:
        return True
    return all(not (b.get(k) or 0) for k in ("net_liq", "cash", "market_value", "buying_power"))


def parse_balance(text: str) -> dict:
    out: dict = {}
    for key, label in BALANCE_FIELDS.items():
        m = re.search(rf"^\s*{re.escape(label)}:\s*(\S+)", text, re.M)
        if m:
            out[key] = m.group(1) if key == "currency" else _num(m.group(1))
    if out.get("net_liq") is None and out.get("cash") is not None:
        out["net_liq"] = (out.get("cash") or 0) + (out.get("market_value") or 0)
        out["net_liq_derived"] = True   # S7: not a real Net Liquidation; money triggers ignore it
    return out


def _option_rows(text: str) -> list[dict]:
    """Option positions from the positions text, using the same one-row-per-line parser the
    option exit manager relies on (broker.parsers.parse_option_positions expects a block layout
    the broker does not print). [] on any problem: the dashboard must not break on an odd row."""
    try:
        from .engine.option_exits import parse_held_options
        from .engine.util import parse_occ
        rows = []
        for h in parse_held_options(text or ""):
            occ = parse_occ(h.symbol) or {}
            upl = (round((h.mark - h.cost) * h.qty * 100, 2)
                   if h.mark is not None and h.cost is not None else None)
            rows.append({"symbol": h.symbol, "underlying": occ.get("underlying"),
                         "option_type": occ.get("option_type"), "strike": occ.get("strike"),
                         "expiration": occ.get("expiration"), "qty": h.qty,
                         "cost_basis": h.cost, "mark_price": h.mark, "unrealized_pnl": upl})
        return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("option rows build failed: %s", exc)
        return []


def parse_position_rows(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if "Leg:" in line or "Symbol:" not in line:
            continue
        kind = re.search(r"\bType:\s*(\S+)", line)
        if kind and kind.group(1).upper() not in {"EQUITY", "STOCK", "ETF"}:
            continue

        def field(label: str, pattern: str = r"(-?[\d.,]+)"):
            m = re.search(rf"(?<!Available )\b{re.escape(label)}:\s*{pattern}", line)
            return m.group(1) if m else None

        sym, qty = field("Symbol", r"(\S+)"), _num(field("Qty") or "")
        if not sym or qty is None:
            continue
        cost, last = _num(field("Cost") or ""), _num(field("Last") or "")
        mv = _num(field("Market Value") or "")
        upl = _num(field("Unrealized P&L") or "")
        if mv is None and last is not None:
            mv = round(qty * last, 2)       # the US API does not report market value
        if upl is None and None not in (last, cost):
            upl = round((last - cost) * qty, 2)
        rows.append({"symbol": sym.upper(), "qty": qty, "cost": cost, "last": last,
                     "market_value": mv, "unrealized": upl,
                     "unrealized_pct": round((last / cost - 1) * 100, 3)
                     if last and cost else None})
    return rows


def swing_rows(rows: list[dict], reserved: dict[str, float] | None) -> list[dict]:
    """Position rows with the multi-strategy sleeves' shares removed (scaled value and P&L), so
    Today's protection ladder lists swing positions only. Rows left at 0 shares drop out."""
    if not reserved:
        return rows
    out = []
    for r in rows:
        held = float(r.get("qty") or 0)
        res = float(reserved.get(r["symbol"], 0.0))
        if res <= 0 or held <= 0:
            out.append(r)
            continue
        left = held - res
        if left < 1e-9:
            continue
        f = left / held
        out.append({**r, "qty": left, "sleeve_qty": res,
                    "market_value": round(r["market_value"] * f, 2) if r.get("market_value") is not None else None,
                    "unrealized": round(r["unrealized"] * f, 2) if r.get("unrealized") is not None else None})
    return out


_ORDER_KEYS = {
    "Order ID": "order_id", "Symbol": "symbol", "Side": "side", "Status": "status",
    "Order Type": "order_type", "Total Quantity": "quantity", "Filled Quantity": "filled_qty",
    "Limit Price": "limit_price", "Stop Price": "stop_price", "Filled Price": "filled_price",
    "Time In Force": "tif", "Place Time": "placed_at", "Filled Time": "filled_at",
    "Average Fill Price": "avg_fill_price", "Commission": "commission",
    "Combo Type": "combo_type", "Client Order ID": "client_order_id",
}
_NUMERIC = {"quantity", "filled_qty", "limit_price", "stop_price", "filled_price", "avg_fill_price", "commission"}


def parse_orders(text: str) -> list[dict]:
    """Every order (including each leg of a combo) from get_open_orders / history."""
    orders: list[dict] = []
    group, combo, cur = None, None, None
    for line in text.splitlines():
        if m := re.match(r"^Client Order ID:\s*(\S+)", line):
            group, cur = m.group(1), None
        elif m := re.match(r"^Combo Type:\s*(\S+)", line):
            combo = m.group(1)
        elif m := re.match(r"^\s{2,}Client Order ID:\s*(\S+)", line):
            cur = {"client_order_id": m.group(1), "group_id": group, "combo_type": combo}
            orders.append(cur)
        elif cur is not None and (m := re.match(r"^\s{2,}([A-Za-z ]+?):\s*(.*)$", line)):
            key = _ORDER_KEYS.get(m.group(1).strip())
            if key:
                value = m.group(2).strip()
                value = None if value in ("", "N/A", "None") else value
                cur[key] = _num(value) if key in _NUMERIC and value else value
    return orders


def _age(row: dict) -> float:
    """Seconds since a protective-order row last changed."""
    stamp = datetime.fromisoformat(row.get("updated_at") or row["created_at"])
    return (now_utc() - stamp).total_seconds()
