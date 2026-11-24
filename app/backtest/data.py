"""Daily-bar cache for the backtester.

Bars are stored as CSV under ``BACKTEST_DATA_DIR`` (``data/bars/<SYMBOL>.csv`` by
default), so a backtest is reproducible offline and a broker outage cannot make
a past run disappear. Fetches are sequential with a delay, skip symbols whose
cache is already long enough, and a single failing symbol never stops the run.

A user can also point ``csv_dir`` at a folder of ``<SYMBOL>.csv`` files with
``date,open,high,low,close,volume`` columns (extra columns are ignored). That is
the supported path when Webull's history is shorter than needed.
"""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)

FIELDS = ("date", "open", "high", "low", "close", "volume")
_ALIASES = {
    "date": {"date", "day", "time", "datetime", "timestamp"},
    "open": {"open", "o"},
    "high": {"high", "h"},
    "low": {"low", "l"},
    "close": {"close", "c", "adj close", "adj_close"},
    "volume": {"volume", "vol", "v"},
}


def _parse_day(value: str) -> datetime:
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.strptime(text[:10], "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _column_map(header: list[str]) -> dict[str, str]:
    lower = {h.strip().lower(): h for h in header}
    out: dict[str, str] = {}
    for target, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in lower:
                out[target] = lower[alias]
                break
    return out


def read_bars_csv(path: Path, symbol: str = "") -> list[dict]:
    """Read one CSV of daily OHLCV bars, oldest first."""
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _column_map(reader.fieldnames or [])
        missing = [f for f in ("date", "open", "high", "low", "close") if f not in cols]
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
        for raw in reader:
            try:
                bar = {
                    "time": _parse_day(raw[cols["date"]]),
                    "open": float(raw[cols["open"]]),
                    "high": float(raw[cols["high"]]),
                    "low": float(raw[cols["low"]]),
                    "close": float(raw[cols["close"]]),
                    "volume": float(raw[cols.get("volume", "")] or 0)
                    if cols.get("volume") else 0.0,
                }
            except (KeyError, TypeError, ValueError):
                continue
            if bar["high"] < bar["low"] or bar["close"] <= 0:
                continue
            rows.append(bar)
    rows.sort(key=lambda b: b["time"])
    return rows


def write_bars_csv(path: Path, bars: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        for b in bars:
            dt = b["time"]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            writer.writerow([
                dt.astimezone(timezone.utc).date().isoformat(),
                b["open"], b["high"], b["low"], b["close"], b.get("volume", 0),
            ])
    tmp.replace(path)


class BarStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.csv"

    def load(self, symbol: str) -> list[dict]:
        path = self.path(symbol)
        if not path.exists():
            return []
        try:
            return read_bars_csv(path, symbol)
        except (OSError, ValueError) as e:
            log.warning("Could not read cached bars for %s: %s", symbol, e)
            return []

    def save(self, symbol: str, bars: list[dict]) -> None:
        if bars:
            write_bars_csv(self.path(symbol), bars)

    def load_all(self, symbols: list[str]) -> dict[str, list[dict]]:
        return {s.upper(): self.load(s) for s in symbols}


def merge_bars(old: list[dict], new: list[dict]) -> list[dict]:
    """Union by calendar day; freshly fetched rows win when both exist."""
    by_day: dict[object, dict] = {}
    for bar in old:
        by_day[bar["time"].date()] = bar
    for bar in new:
        by_day[bar["time"].date()] = bar
    return [by_day[d] for d in sorted(by_day)]


async def refresh_from_broker(store: BarStore, symbols: list[str], broker, settings: Settings,
                              *, force: bool = False) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Fetch missing/refresh stale symbols sequentially, one failure at a time."""
    wanted = settings.backtest_fetch_count
    minimum = max(settings.backtest_min_bars, 200)
    bars: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for i, raw_symbol in enumerate(symbols):
        symbol = raw_symbol.upper()
        cached = store.load(symbol)
        enough = len(cached) >= minimum and not force
        if enough and cached:
            bars[symbol] = cached
            continue
        if i and settings.backtest_fetch_delay_seconds:
            await asyncio.sleep(settings.backtest_fetch_delay_seconds)
        try:
            fetched = await broker.daily_bars(symbol, wanted)
        except Exception as e:  # noqa: BLE001 - one symbol must not stop the run
            errors[symbol] = f"{type(e).__name__}: {e}"
            log.warning("Backtest data fetch failed for %s: %s", symbol, e)
            if cached:
                bars[symbol] = cached
            continue
        merged = merge_bars(cached, fetched)
        if merged:
            store.save(symbol, merged)
        bars[symbol] = merged
        log.info("Backtest bars for %s: %s fetched, %s cached", symbol, len(fetched), len(merged))
    return bars, errors


def load_csv_dir(csv_dir: Path, symbols: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for symbol in symbols:
        path = Path(csv_dir) / f"{symbol.upper()}.csv"
        if not path.exists():
            continue
        try:
            out[symbol.upper()] = read_bars_csv(path, symbol)
        except (OSError, ValueError) as e:
            log.warning("Could not load %s: %s", path, e)
    return out


def load_cached_bars(settings: Settings, symbols: list[str], *,
                     csv_dir: Path | None = None) -> dict[str, list[dict]]:
    """Load only what is already on disk (cache plus an optional CSV folder)."""
    store = BarStore(settings.backtest_data_dir)
    bars = store.load_all(symbols)
    if csv_dir:
        for symbol, rows in load_csv_dir(Path(csv_dir), symbols).items():
            bars[symbol] = merge_bars(bars.get(symbol, []), rows)
    return {s: rows for s, rows in bars.items() if rows}


async def load_bars(settings: Settings, symbols: list[str], *, broker=None, force: bool = False,
                    csv_dir: Path | None = None) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Load cached bars, optionally refreshing from the broker, plus a CSV folder."""
    bars = load_cached_bars(settings, symbols, csv_dir=csv_dir)
    errors: dict[str, str] = {}
    if broker is not None:
        store = BarStore(settings.backtest_data_dir)
        fetched, errors = await refresh_from_broker(store, symbols, broker, settings,
                                                    force=force)
        for symbol, rows in fetched.items():
            bars[symbol] = merge_bars(bars.get(symbol, []), rows)
    return {s: rows for s, rows in bars.items() if rows}, errors
