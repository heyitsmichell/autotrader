"""Target-weight rebalance diff and order chunking (pure)."""
from __future__ import annotations

import math


def diff(holdings, targets, equity, prices, min_trade_usd=1000, band_pct=0):
    sells, buys = [], []
    for sym in sorted(set(holdings) | set(targets)):
        px = prices.get(sym)
        if not px or px <= 0:
            continue
        held = int(holdings.get(sym, 0) or 0)
        w = targets.get(sym, 0.0)
        tgt = math.floor(equity * w / px) if w > 0 else 0
        delta = tgt - held
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
