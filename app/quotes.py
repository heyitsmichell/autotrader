"""Quote value type shared by the feed, broker snapshots, the option gate and the risk loop.

Every price used for a decision carries ``as_of`` (UTC epoch seconds of when the venue/broker
produced it, or when we fetched it if the source has no timestamp). Decision code must call
``is_fresh(max_age)`` and fail closed when it returns False.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    as_of: float = 0.0          # UTC epoch seconds
    source: str = ""            # "stream" | "poll" | "snapshot" | "mock" | ...
    # Options extras (None for equities or when the source does not provide them)
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    stale: bool = False         # equity: outside CORE with only the previous regular close

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.as_of) if self.as_of else math.inf

    def is_fresh(self, max_age_seconds: float, now: float | None = None) -> bool:
        return self.age(now) <= max_age_seconds

    @property
    def mid(self) -> float | None:
        b, a = _num(self.bid), _num(self.ask)
        if b is None or a is None or b <= 0 or a <= 0 or a < b:
            return None
        return (a + b) / 2

    @property
    def two_sided(self) -> bool:
        """True when bid and ask are both positive and the market is neither crossed nor locked."""
        b, a = _num(self.bid), _num(self.ask)
        return b is not None and a is not None and b > 0 and a > 0 and a > b

    @property
    def spread_pct(self) -> float | None:
        m = self.mid
        if m is None:
            return None
        return (float(self.ask) - float(self.bid)) / m
