"""TTL-cached per-symbol signal snapshots (the hand-off between cold and hot paths, M4).

Cold-path workers (signal refresh: TA, sentiment, ML scoring) write here. The hot path
(the slow proposal cycle under ``_run_lock``, the fast loop) only reads. ``get`` returns None
for a symbol that is missing or older than ``max_age`` seconds, so a stalled worker makes the
hot path fail closed (no new entries) instead of trading on old data.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable


class SignalStore:
    def __init__(self, ttl_seconds: float = 600.0, clock: Callable[[], float] = time.time) -> None:
        self.ttl = float(ttl_seconds)
        self._clock = clock
        self._items: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self.last_put: float | None = None

    def put(self, symbol: str, signal: dict, as_of: float | None = None) -> None:
        now = self._clock()
        with self._lock:
            self._items[symbol.upper()] = (float(as_of if as_of is not None else now), signal)
            self.last_put = now

    def put_many(self, signals: Iterable[dict], as_of: float | None = None) -> int:
        n = 0
        for sig in signals or []:
            sym = (sig or {}).get("symbol")
            if sym:
                self.put(sym, sig, as_of)
                n += 1
        return n

    def get(self, symbol: str, max_age: float | None = None) -> dict | None:
        """The stored signal, or None when missing or older than ``max_age`` (default: the TTL)."""
        limit = self.ttl if max_age is None else float(max_age)
        with self._lock:
            hit = self._items.get(symbol.upper())
        if hit is None:
            return None
        as_of, sig = hit
        return sig if self._clock() - as_of <= limit else None

    def age(self, symbol: str) -> float | None:
        with self._lock:
            hit = self._items.get(symbol.upper())
        return None if hit is None else max(0.0, self._clock() - hit[0])

    def snapshot(self, symbols: Iterable[str] | None = None,
                 max_age: float | None = None) -> list[dict]:
        """Fresh signals (all, or for the given symbols), in the order of ``symbols``."""
        if symbols is None:
            with self._lock:
                names = list(self._items)
        else:
            names = [s.upper() for s in symbols]
        out = []
        for name in names:
            sig = self.get(name, max_age)
            if sig is not None:
                out.append(sig)
        return out

    def missing(self, symbols: Iterable[str], max_age: float | None = None) -> list[str]:
        return [s for s in symbols if self.get(s, max_age) is None]

    def newest_age(self) -> float | None:
        with self._lock:
            if not self._items:
                return None
            return max(0.0, self._clock() - max(a for a, _ in self._items.values()))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.last_put = None

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
