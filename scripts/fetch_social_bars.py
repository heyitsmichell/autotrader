#!/usr/bin/env python3
"""Fetch extra (leveraged/vol) bars for the social-strategy lab.
Usage: python scripts/fetch_social_bars.py [--symbols TQQQ,UPRO,...] [--start 2005-01-01]
Writes data/bars_yahoo_ext/social/<SYM>.csv (separate dir so other labs' universe globs are unaffected).
Index tickers (^VIX) are stored as VIX.csv. End is capped at 2025-11-24 exclusive (locked holdout)."""
import argparse
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "bars_yahoo_ext" / "social"
END = "2025-11-24"
DEFAULT = ("TQQQ UPRO SSO QLD TMF UBT SOXL TECL SPXL SVXY VIXY VXX UVXY SQQQ SH SPXU TNA EDV "
           "SHY SCHD SMH XLK ^VIX ^VIX3M ^VIX9D ^VXN ^GSPC ^NDX").split()

def main():
    import yfinance as yf
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT))
    ap.add_argument("--start", default="2005-01-01")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for s in [x.strip() for x in a.symbols.split(",") if x.strip()]:
        name = s.lstrip("^")
        p = OUT / f"{name}.csv"
        try:
            d = yf.download(s, start=a.start, end=END, interval="1d", auto_adjust=True, progress=False)
        except Exception as e:
            print(s, "FAIL", e); continue
        if d is None or len(d) < 50:
            print(s, "MISSING"); continue
        if hasattr(d.columns, "levels"):
            d.columns = d.columns.get_level_values(0)
        o = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"]).copy()
        o.columns = ["open", "high", "low", "close", "volume"]; o.index.name = "date"
        o.index = o.index.tz_localize(None) if o.index.tz else o.index
        o.to_csv(p); print(s, o.index[0].date(), o.index[-1].date(), len(o))

if __name__ == "__main__":
    main()
