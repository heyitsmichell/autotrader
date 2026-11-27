"""Black-Scholes price and Greeks (no dividends, pure math). Rate is a documented constant."""
from __future__ import annotations

import math

RATE = 0.04
_EPS = 1e-9


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def intrinsic(S: float, K: float, right: str) -> float:
    return max(K - S, 0.0) if right == "P" else max(S - K, 0.0)


def _d1d2(S, K, T, sig, r):
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return d1, d1 - sig * math.sqrt(T)


def bs_price(S: float, K: float, T: float, sig: float, right: str, r: float = RATE) -> float:
    """Option price per share. T in years; T <= 0 or sig <= 0 gives intrinsic value."""
    if T <= _EPS or sig <= _EPS or S <= 0 or K <= 0:
        return intrinsic(S, K, right)
    d1, d2 = _d1d2(S, K, T, sig, r)
    if right == "P":
        return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def greeks(S: float, K: float, T: float, sig: float, right: str, r: float = RATE) -> dict:
    """Per-share Greeks: delta, gamma (per $), vega (per 1.0 of vol), theta (per year)."""
    if T <= _EPS or sig <= _EPS or S <= 0 or K <= 0:
        itm = (S < K) if right == "P" else (S > K)
        return {"delta": (-1.0 if right == "P" else 1.0) if itm else 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    d1, d2 = _d1d2(S, K, T, sig, r)
    pdf = _npdf(d1)
    gamma = pdf / (S * sig * math.sqrt(T))
    vega = S * pdf * math.sqrt(T)
    common = -S * pdf * sig / (2.0 * math.sqrt(T))
    if right == "P":
        delta = _ncdf(d1) - 1.0
        theta = common + r * K * math.exp(-r * T) * _ncdf(-d2)
    else:
        delta = _ncdf(d1)
        theta = common - r * K * math.exp(-r * T) * _ncdf(d2)
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def implied_vol(mark: float, S: float, K: float, T: float, right: str, r: float = RATE,
                lo: float = 0.01, hi: float = 5.0) -> float | None:
    """Bisection on bs_price; None when T <= 0 or the mark is outside [price(lo), price(hi)] (e.g. below intrinsic)."""
    if not mark or mark <= 0 or T <= _EPS or S <= 0 or K <= 0:
        return None
    if not (bs_price(S, K, T, lo, right, r) <= mark <= bs_price(S, K, T, hi, right, r)):
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, mid, right, r) < mark:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return 0.5 * (lo + hi)
