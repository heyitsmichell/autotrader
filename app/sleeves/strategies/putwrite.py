"""Cash-secured put-write helpers (pure)."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta


DTE_MIN = 21
DTE_MAX = 45
EST_VOL = 0.25           # vol used to estimate the target strike before quotes are fetched
FALLBACK_STRIKE_PCT = 0.96
SELL_SPREAD_FRAC = 0.25  # sell-to-open: mid minus this fraction of the spread
TICK_BIG, TICK_SMALL, TICK_SPLIT = 0.05, 0.01, 3.00


def tick_for(px: float, tick=None) -> float:
    if tick:
        return float(tick)
    return TICK_BIG if px >= TICK_SPLIT else TICK_SMALL


def round_tick(px: float, mode: str = "nearest", tick=None) -> float:
    """Round to the contract tick ($0.05 from $3.00, else $0.01, or the quote's own tick)."""
    t = tick_for(px, tick)
    q = px / t
    n = math.ceil(q - 1e-9) if mode == "up" else math.floor(q + 1e-9) if mode == "down" else round(q)
    return round(max(n, 1) * t, 2)


def sell_open_limit(bid: float, ask: float, tick=None) -> float:
    """Nearest tick to the target price, never below the bid."""
    px = round_tick((bid + ask) / 2 - SELL_SPREAD_FRAC * (ask - bid), "nearest", tick)
    return max(px, round_tick(bid, "up", tick))


def pick_contract(chain, spot, today, target_delta=0.30, dte=30, dte_min=DTE_MIN, dte_max=DTE_MAX):
    puts = [r for r in chain if str(r.get("option_type", "")).upper() == "PUT"]
    ok = {}
    for r in puts:
        d = (r["expiration"] - today).days
        if dte_min <= d <= dte_max:
            ok.setdefault(r["expiration"], d)
    if not ok:
        return None
    exp = min(ok, key=lambda e: (abs(ok[e] - dte), e))
    rows = [r for r in puts if r["expiration"] == exp and (r.get("bid") or 0) > 0]
    if not rows:
        return None
    with_delta = [r for r in rows if r.get("delta") is not None]
    if with_delta:
        best = min(with_delta, key=lambda r: abs(abs(r["delta"]) - target_delta))
    else:
        best = min(rows, key=lambda r: abs(r["strike"] - spot * FALLBACK_STRIKE_PCT))
    return {**best, "dte": ok[exp]}


def contracts_for(cash, strike) -> int:
    if not strike or strike <= 0 or not cash or cash <= 0:
        return 0
    return max(0, math.floor(cash / (strike * 100)))


def should_close(put, spot, today: date, now_et: datetime) -> bool:
    exp = put["expiration"]
    if spot >= put["strike"]:
        return False
    close = _close_of(exp)
    start = (datetime.combine(exp, close) - timedelta(minutes=30)).time()
    return exp < today or (exp == today and now_et.time() >= start)


def _close_of(day: date) -> time:
    """That day's session close (13:00 on early closes); 16:00 if unknown."""
    from ... import market
    return market.session_close(day) or time(16, 0)


def is_expired(put, today: date, now_et: datetime) -> bool:
    exp = put["expiration"]
    return exp < today or (exp == today and now_et.time() >= _close_of(exp))
