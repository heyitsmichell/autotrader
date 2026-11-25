"""Cash-secured put-write helpers (pure)."""
from __future__ import annotations

import math
from datetime import date, datetime, time


def pick_contract(chain, spot, today, target_delta=0.30, dte=30, dte_min=21, dte_max=45):
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
        best = min(rows, key=lambda r: abs(r["strike"] - spot * 0.96))
    return {**best, "dte": ok[exp]}


def contracts_for(cash, strike) -> int:
    if not strike or strike <= 0 or not cash or cash <= 0:
        return 0
    return max(0, math.floor(cash / (strike * 100)))


def should_close(put, spot, today: date, now_et: datetime) -> bool:
    exp = put["expiration"]
    if spot >= put["strike"]:
        return False
    return exp < today or (exp == today and now_et.time() >= time(15, 30))


def is_expired(put, today: date, now_et: datetime) -> bool:
    exp = put["expiration"]
    return exp < today or (exp == today and now_et.time() >= time(16, 0))
