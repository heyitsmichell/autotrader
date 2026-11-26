"""Target-weight rebalance diff and order chunking (pure)."""
from __future__ import annotations

import math


def missing_prices(holdings, targets, prices):
    """Held or target symbols (held qty > 0 or target weight > 0) with no usable price."""
    out = []
    for sym in sorted(set(holdings) | set(targets)):
        if not (float(holdings.get(sym, 0) or 0) > 0 or float(targets.get(sym, 0) or 0) > 0):
            continue
        px = prices.get(sym)
        if not px or px <= 0:
            out.append(sym)
    return out


def diff(holdings, targets, equity, prices, min_trade_usd=1000, band_pct=0):
    sells, buys = [], []
    for sym in sorted(set(holdings) | set(targets)):
        px = prices.get(sym)
        if not px or px <= 0:
            continue
        held = round(float(holdings.get(sym, 0) or 0), 6)
        w = targets.get(sym, 0.0)
        tgt = math.floor(equity * w / px) if w > 0 else 0   # whole shares for buys
        delta = round(tgt - held, 6)
        if delta == 0:
            continue
        if sym in targets:  # symbols dropped from targets are full sells regardless of thresholds
            if abs(delta * px) < min_trade_usd:
                continue
            if band_pct > 0 and equity > 0 and abs(held * px / equity - w) < band_pct / 100:
                continue
        (buys if delta > 0 else sells).append((sym, "BUY" if delta > 0 else "SELL", abs(delta)))
    return sells + buys


def chunks(qty, price, max_order_usd, max_order_qty):
    if qty <= 0:
        return []
    cap = qty
    if max_order_qty and max_order_qty > 0:
        cap = min(cap, int(max_order_qty))
    if max_order_usd and max_order_usd > 0 and price > 0:
        cap = min(cap, int(max_order_usd // price))
    cap = max(1, cap)
    out = [cap] * (qty // cap)
    if qty % cap:
        out.append(qty % cap)
    return out
