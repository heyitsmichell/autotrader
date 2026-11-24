"""Synthetic prices (sum of slow and fast waves, so there are multi-day trends)."""

from __future__ import annotations

import math
import time
import zlib

BASE_PRICES = {"AAPL": 230.0, "MSFT": 430.0, "NVDA": 140.0, "SPY": 590.0, "QQQ": 510.0,
               "AMZN": 200.0, "GOOGL": 175.0, "META": 580.0, "TSLA": 250.0}
# Symbols eligible for fractional trading (mock)
DAY = 86_400


# -- simulated market ----------------------------------------------------------
def price_of(symbol: str, at: float | None = None) -> float:
    t = at if at is not None else time.time()
    seed = zlib.crc32(symbol.encode())
    base = BASE_PRICES.get(symbol, 50.0 + seed % 200)
    ph = (seed % 1000) / 1000 * 2 * math.pi
    tau = 2 * math.pi
    drift = (0.07 * math.sin(tau * t / (DAY * 61) + ph)          # multi-week trend
             + 0.04 * math.sin(tau * t / (DAY * 17.3) + ph * 1.7)
             + 0.02 * math.sin(tau * t / (DAY * 6.1) + ph * 2.3)  # swing
             + 0.008 * math.sin(tau * t / DAY * 3 + ph)           # intraday
             + 0.004 * math.sin(tau * t / 900 + ph * 2)
             + 0.0015 * math.sin(tau * t / 97 + ph * 3))
    return round(base * (1 + drift), 2)
