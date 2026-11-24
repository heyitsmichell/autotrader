#!/usr/bin/env python3
"""Fetch daily OHLCV for the live universe from Yahoo (yfinance) into a CSV cache.

Format is what app/backtest/data.py reads: date,open,high,low,close,volume.
Prices are split/dividend adjusted (auto_adjust=True). One batched download, politely.

  .venv/Scripts/python.exe scripts/fetch_yahoo_bars.py --years 5 --out data/bars_yahoo
Never overwrites a file with fewer rows than it already has.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def live_symbols() -> list[str]:
    from app.config import load_settings
    from app.universe import get_predefined_universe
    s = load_settings()
    syms: list[str] = []
    for name in (s.watchlist_universes if hasattr(s, "watchlist_universes") else ["tech_semis"]):
        syms += get_predefined_universe(name)
    syms += [x.upper() for x in s.watchlist] + ["SPY", "QQQ"]
    return list(dict.fromkeys(syms))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--out", default=str(ROOT / "data" / "bars_yahoo"))
    ap.add_argument("--batch", type=int, default=25)
    args = ap.parse_args()
    import yfinance as yf
    from app.backtest.data import BarStore, read_bars_csv
    from datetime import datetime, timezone

    out = Path(args.out)
    store = BarStore(out)
    syms = live_symbols()
    print(f"{len(syms)} symbols")
    missing: list[str] = []
    for i in range(0, len(syms), args.batch):
        chunk = syms[i:i + args.batch]
        df = yf.download(chunk, period=f"{args.years}y", interval="1d", auto_adjust=True,
                         group_by="ticker", threads=False, progress=False)
        for sym in chunk:
            try:
                d = df[sym] if len(chunk) > 1 else df
                d = d.dropna(subset=["Open", "High", "Low", "Close"])
            except Exception:
                d = None
            if d is None or len(d) < 50:
                missing.append(sym)
                continue
            bars = []
            for ts, r in d.iterrows():
                dt = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
                bars.append({"time": dt, "open": round(float(r["Open"]), 4), "high": round(float(r["High"]), 4),
                             "low": round(float(r["Low"]), 4), "close": round(float(r["Close"]), 4),
                             "volume": float(r.get("Volume", 0) or 0)})
            old = store.load(sym)
            if len(old) > len(bars):
                print(f"keep existing {sym} ({len(old)} > {len(bars)})")
                continue
            store.save(sym, bars)
            print(f"{sym}: {len(bars)} bars {bars[0]['time'].date()}..{bars[-1]['time'].date()}")
        time.sleep(3)
    print("missing/no data:", missing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
