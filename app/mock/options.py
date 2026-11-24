"""Mock option pricing (Black-Scholes approximation) and OCC symbols."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from .quotes import price_of



# Fallback for scipy.stats.norm.cdf using math.erf
def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function using error function."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))



def option_symbol(underlying: str, expiration: str, opt_type: str, strike: float) -> str:
    """Generate OCC-style option symbol: AAPL240119C00150000"""
    # expiration format: YYYY-MM-DD -> YYMMDD
    exp = expiration.replace("-", "")[2:]
    cp = "C" if opt_type.upper() == "CALL" else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying.upper()}{exp}{cp}{strike_int:08d}"


def option_price(underlying: str, strike: float, expiration: str, opt_type: str, at: float | None = None) -> tuple[float, float]:
    """Return (bid, ask) for an option using Black-Scholes approximation."""
    from math import log, sqrt, exp
    try:
        S = price_of(underlying, at)
        K = strike
        # Time to expiry in years
        exp_dt = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        # Ensure exp_dt is timezone-aware (UTC)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        now = datetime.fromtimestamp(at or time.time(), timezone.utc)
        T = max((exp_dt - now).total_seconds() / (365.25 * 86400), 1/365.25)
        r = 0.05  # risk-free rate
        sigma = 0.30  # implied volatility (mock constant)
        d1 = (log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt(T))
        d2 = d1 - sigma * sqrt(T)
        if opt_type.upper() == "CALL":
            theoretical = S * norm_cdf(d1) - K * exp(-r * T) * norm_cdf(d2)
        else:
            theoretical = K * exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
        theoretical = max(theoretical, 0.01)
        spread = theoretical * 0.02  # 2% spread
        bid = round(theoretical - spread / 2, 2)
        ask = round(theoretical + spread / 2, 2)
        return max(bid, 0.01), max(ask, 0.01)
    except Exception:
        # Fallback
        return 0.50, 0.55
