#!/usr/bin/env python3
"""Fetch ~15y adjusted daily bars (S&P500 current list + ETFs) into data/bars_yahoo_ext/.
Run with the project venv (has yfinance, pandas, lxml?)."""
import io, sys, time, datetime as dt, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "bars_yahoo_ext"
ETFS = "XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY SPY QQQ IWM TLT IEF GLD BIL SHV SGOV".split()

def sp500():
    import pandas as pd
    req = urllib.request.Request("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                                 headers={"User-Agent": "Mozilla/5.0 research-script"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf8")
    df = pd.read_html(io.StringIO(html))[0]
    df.to_csv(OUT / f"sp500_list_{dt.date.today()}.csv", index=False)
    return [s.replace(".", "-") for s in df["Symbol"].astype(str)]

EXTRA = ("VEA EFA VWO EEM BND AGG LQD HYG TIP SHY VNQ IWD IWF MTUM USMV QUAL RSP DBC PDBC SPLV VTI VGK EWJ IJR VBR VUG VTV "
         "SLV SIVR GDX GDXJ UUP UDN FXE FXY FXB FXA FXC FXF").split()
END = "2025-11-24"  # exclusive: locked holdout starts after 2025-11-23


def main_extra():
    import yfinance as yf
    OUT.mkdir(parents=True, exist_ok=True)
    for s in EXTRA:
        p = OUT / f"{s}.csv"
        if p.exists():
            print(s, "exists, skip"); continue
        d = yf.download(s, start="2005-01-01", end=END, interval="1d", auto_adjust=True, progress=False)
        if d is None or len(d) < 50:
            print(s, "MISSING"); continue
        if hasattr(d.columns, "levels"):
            d.columns = d.columns.get_level_values(0)
        o = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "Close"]).copy()
        o.columns = ["open", "high", "low", "close", "volume"]; o.index.name = "date"
        o.to_csv(p); print(s, o.index[0].date(), o.index[-1].date(), len(o))


def main():
    import yfinance as yf
    OUT.mkdir(parents=True, exist_ok=True)
    syms = list(dict.fromkeys(ETFS + sp500()))
    print(len(syms), "symbols"); missing = []
    B = 20
    for i in range(0, len(syms), B):
        ch = syms[i:i+B]
        try:
            df = yf.download(ch, start="2010-01-01", interval="1d", auto_adjust=True,
                             group_by="ticker", threads=False, progress=False)
        except Exception as e:
            print("batch fail", e); missing += ch; continue
        for s in ch:
            try:
                d = df[s].dropna(subset=["Open", "High", "Low", "Close"])
            except Exception:
                d = None
            if d is None or len(d) < 50:
                missing.append(s); continue
            p = OUT / f"{s}.csv"
            if p.exists() and sum(1 for _ in open(p)) - 1 >= len(d):
                continue
            o = d[["Open", "High", "Low", "Close", "Volume"]].copy()
            o.columns = ["open", "high", "low", "close", "volume"]; o.index.name = "date"
            o.index = o.index.tz_localize(None) if o.index.tz else o.index
            o.to_csv(p)
        print(i + B, "done"); time.sleep(1.5)
    print("missing:", missing)

if __name__ == "__main__":
    main_extra() if "--extra" in sys.argv else main()

