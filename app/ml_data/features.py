"""Point-in-time daily features and SPY regime for a (symbol, date). Bars up to and including the date only."""
from __future__ import annotations

import bisect
import csv
from datetime import date, datetime, timezone
from pathlib import Path

from .. import indicators

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIRS = [ROOT / "data" / "bars_yahoo", ROOT / "data" / "bars_yahoo_ext"]
MAX_STALE_DAYS = 5   # a last bar older than this vs. the requested date counts as "no data"


def _d(x) -> str:
    return str(x)[:10]


def _read(path: Path) -> list[dict]:
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                t = datetime.fromisoformat(r["date"][:10]).replace(tzinfo=timezone.utc)
                out.append({"time": t, "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                            "close": float(r["close"]), "volume": float(r.get("volume") or 0)})
            except (ValueError, KeyError):
                continue
    return out


class BarStore:
    """Loads daily bars per symbol (primary dir, then fallback dirs) and caches feature series."""

    def __init__(self, dirs=None):
        self.dirs = [Path(d) for d in (dirs or DEFAULT_DIRS)]
        self._bars: dict[str, list[dict]] = {}
        self._days: dict[str, list[str]] = {}
        self._feats: dict[str, list[dict]] = {}

    def bars(self, symbol: str) -> list[dict]:
        if symbol not in self._bars:
            b: list[dict] = []
            for d in self.dirs:
                p = d / f"{symbol}.csv"
                if p.exists():
                    b = _read(p)
                    if len(b) >= 2:
                        break
            self._bars[symbol] = b
            self._days[symbol] = [x["time"].date().isoformat() for x in b]
        return self._bars[symbol]

    def days(self, symbol: str) -> list[str]:
        self.bars(symbol)
        return self._days[symbol]

    def series(self, symbol: str) -> list[dict]:
        if symbol not in self._feats:
            b = self.bars(symbol)
            spy = self.bars("SPY") if symbol != "SPY" else None
            self._feats[symbol] = indicators.feature_series(b, spy) if len(b) >= 2 else []
        return self._feats[symbol]

    def index_at(self, symbol: str, day: str) -> int | None:
        days = self.days(symbol)
        i = bisect.bisect_right(days, day) - 1
        if i < 0:
            return None
        if (date.fromisoformat(day) - date.fromisoformat(days[i])).days > MAX_STALE_DAYS:
            return None
        return i


_DEFAULT = BarStore()


def features_at(symbol: str, day, store: BarStore | None = None, truncate: bool = False) -> dict | None:
    """The app's daily feature set as of the close of ``day`` (last bar <= day), None without data.

    feature_series row i only uses bars[0..i], so the cached full series is point-in-time. truncate=True
    recomputes from bars up to ``day`` only (used by the tests to prove it)."""
    st = store or _DEFAULT
    day = _d(day)
    i = st.index_at(symbol, day)
    if i is None:
        return None
    if truncate:
        b = st.bars(symbol)[: i + 1]
        spy = None
        if symbol != "SPY":
            spy = [x for x in st.bars("SPY") if x["time"].date().isoformat() <= day]
        rows = indicators.feature_series(b, spy)
        return dict(rows[-1]) if rows else None
    s = st.series(symbol)
    return dict(s[i]) if i < len(s) else None


def spy_regime_at(day, store: BarStore | None = None) -> dict:
    """SPY regime on ``day`` aligned by DATE (last SPY bar <= day)."""
    f = features_at("SPY", day, store)
    if not f:
        return {}
    c, s50, s200 = f.get("close"), f.get("sma50"), f.get("sma200")
    trend = None
    if c is not None and s50 is not None and s200 is not None:
        trend = "up" if c > s200 and s50 > s200 else ("down" if c < s200 and s50 < s200 else "flat")
    return {"spy_close": c, "spy_trend": trend, "spy_adx14": f.get("adx14"),
            "spy_pct_vs_sma200": f.get("pct_vs_sma200"), "spy_pct_vs_sma50": f.get("pct_vs_sma50"),
            "spy_rsi14": f.get("rsi14"), "spy_chg_20d_pct": f.get("chg_20d_pct")}


def signal_date_for(entry_day, store: BarStore | None = None) -> str | None:
    """Last SPY trading day strictly before ``entry_day`` (entries fill at the next session's open)."""
    days = (store or _DEFAULT).days("SPY")
    i = bisect.bisect_left(days, _d(entry_day)) - 1
    return days[i] if i >= 0 else None


def hold_excursions(symbol: str, entry_day, exit_day, entry_price, store: BarStore | None = None):
    """(mfe, mae) as fractions of entry_price from daily highs/lows over entry..exit, else (None, None)."""
    st = store or _DEFAULT
    days, b = st.days(symbol), st.bars(symbol)
    if not days or not entry_price:
        return None, None
    e = bisect.bisect_left(days, _d(entry_day))
    if e >= len(days) or (date.fromisoformat(days[e]) - date.fromisoformat(_d(entry_day))).days > MAX_STALE_DAYS:
        return None, None
    x = bisect.bisect_right(days, _d(exit_day)) if exit_day else len(days)
    win = b[e:x]
    if not win:
        return None, None
    return max(w["high"] for w in win) / entry_price - 1, min(w["low"] for w in win) / entry_price - 1
