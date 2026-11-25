#!/usr/bin/env python3
"""Swing universe study: live universe vs all S&P 500 vs dynamic liquidity/near-high universes.

Variants (live variant d, $500k, 0.5% risk, same fees as sleeve_swing_backtest.py):
  A live universe  B all S&P 500 (today's constituents, survivorship-biased)
  C top-100 by 20d $-volume of (SP500 U live), ranks from the PREVIOUS day, entries only
  D C + prev close within 5% of 252d high + max 3 entries-eligible per GICS sector/day
Bars are cut at 2025-11-21 (locked holdout never loaded).   python scripts/swing_universe_study.py
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import shutil
import sys
import time
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import swing_backtest as sb  # noqa: E402
from sleeve_swing_backtest import CASH, CUTOFF, D, SIZE, cut_bars  # noqa: E402

EXT = ROOT / "data" / "bars_yahoo_ext"
OUT = ROOT / "data" / "backtests" / "swing_universe_2026-11-25"
SCRATCH = OUT / "_bars"
WINDOWS = {"full_2011_2025": "2011-01-03", "last3y_2022-11-21_2025-11-21": "2022-11-21"}
TOPN, NEAR_HIGH, SECTOR_CAP = 100, 0.05, 3


def sp500_table() -> pd.DataFrame:
    f = sorted(EXT.glob("sp500_list_*.csv"))[-1]
    t = pd.read_csv(f)
    t["Symbol"] = t["Symbol"].astype(str).str.replace(".", "-", regex=False)
    return t


def universes() -> dict:
    have = {p.stem for p in EXT.glob("*.csv") if not p.name.startswith("sp500_list")}
    live = [u for u in sb.universe_for(sb.build_settings({}, EXT)) if u in have]
    sp = [s for s in sp500_table()["Symbol"] if s in have]
    return {"A": live, "B": sp, "CD": sorted(set(sp) | set(live))}


def make_filter(kind: str, syms: list):
    """Return entry_allowed(day, symbol) using only data up to the previous trading day."""
    px, vol = {}, {}
    for s in syms:
        df = pd.read_csv(SCRATCH / f"{s}.csv", usecols=["date", "close", "volume"], index_col=0, parse_dates=True)
        px[s], vol[s] = df["close"], df["volume"]
    close, volume = pd.DataFrame(px).sort_index(), pd.DataFrame(vol).sort_index()
    dv = (close * volume).rolling(20, min_periods=20).mean()
    hi = close.rolling(252, min_periods=252).max()
    near = close >= (1 - NEAR_HIGH) * hi
    sec = sp500_table().set_index("Symbol")["GICS Sector"].to_dict()
    idx = close.index
    cache: dict = {}

    def allowed_set(day) -> frozenset:
        pos = idx.searchsorted(pd.Timestamp(day)) - 1       # last row strictly before `day`
        if pos < 0:
            return frozenset()
        if pos in cache:
            return cache[pos]
        row = dv.iloc[pos].dropna().sort_values(ascending=False)
        top = list(row.index[:TOPN])
        if kind == "D":
            nr = near.iloc[pos]
            cnt, keep = {}, []
            for s in top:
                if not bool(nr[s]):
                    continue
                g = sec.get(s, "Other")
                if cnt.get(g, 0) < SECTOR_CAP:
                    cnt[g] = cnt.get(g, 0) + 1
                    keep.append(s)
            top = keep
        cache[pos] = frozenset(top)
        return cache[pos]

    def entry_allowed(day, symbol) -> bool:
        return symbol in allowed_set(day)
    return entry_allowed


def run_variant(args):
    name, uni, syms = args
    from app.backtest.engine import run_backtest
    t0 = time.time()
    s = sb.build_settings({**D, **SIZE}, SCRATCH)
    hook = make_filter(name, syms) if name in ("C", "D") else None
    res = run_backtest(s, universe=uni, benchmark_symbol="SPY", sec_fee_rate=2.78e-05, taf_per_share=0.000166,
                       entry_allowed=hook)
    n = min(len(res.equity_dates), len(res.equity_curve))
    return {"name": name, "dates": res.equity_dates[:n], "eq": res.equity_curve[:n],
            "expo": list(res.exposure_curve), "trades": res.trades, "universe_n": len(uni),
            "seconds": round(time.time() - t0, 1)}


def sharpe(r) -> float:
    sd = r.std(ddof=1)
    return float(r.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0


def boot_ci(a, x, block=21, n=2000, seed=7):
    rng = np.random.default_rng(seed)
    L = len(a)
    nb = math.ceil(L / block)
    out = np.empty(n)
    for k in range(n):
        st = rng.integers(0, L - block + 1, nb)
        ix = (st[:, None] + np.arange(block)).ravel()[:L]
        out[k] = sharpe(x[ix]) - sharpe(a[ix])
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))], float((out > 0).mean())


def main() -> int:
    t_start = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    U = universes()
    cut_bars(EXT, SCRATCH, sorted(set(U["CD"]) | set(U["A"]) | set(U["B"]) | {"SPY"}))
    jobs = [("A", U["A"], U["CD"]), ("B", U["B"], U["CD"]), ("C", U["CD"], U["CD"]), ("D", U["CD"], U["CD"])]
    with Pool(4) as p:
        R = {r["name"]: r for r in p.map(run_variant, jobs)}
    mom = pd.read_csv(gzip.open(ROOT / "app" / "sleeves" / "reference" / "momentum.csv.gz", "rt"),
                      index_col=0, parse_dates=True)["ret"]
    summary: dict = {"universe_sizes": {k: len(v) for k, v in U.items()}, "windows": {}}
    series = {}
    for k, r in R.items():
        eq = pd.Series(r["eq"], index=pd.to_datetime(r["dates"]))
        eq = eq[~eq.index.duplicated()]
        series[k] = eq
        with open(OUT / f"equity_{k}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "equity"])
            for d, v in eq.items():
                w.writerow([d.date().isoformat(), round(v, 2)])
    for wname, w0 in WINDOWS.items():
        wd, rets = {}, {}
        for k, r in R.items():
            dates = list(r["dates"])
            i0 = max(1, next(i for i, d in enumerate(dates) if d >= datetime.fromisoformat(w0).date()))
            n = len(r["eq"])
            m = sb.seg_metrics(dates, r["eq"], r["expo"], r["trades"], i0, n, CASH)
            m["trades_per_day"] = m["trades"] / max(1, n - i0 - 1)
            d0 = dates[i0]
            ds = pd.bdate_range(d0, dates[-1])
            cnt = np.zeros(len(ds))
            for t in r["trades"]:
                if t.exit_day:
                    cnt += ((ds.date >= t.entry_day) & (ds.date < t.exit_day))
            m["avg_positions"] = float(cnt.mean())
            m["universe_n"] = r["universe_n"]
            s = series[k]
            rets[k] = s[s.index >= pd.Timestamp(d0)].pct_change().dropna()
            wd[k] = m
        for k in R:
            if k != "A":
                al = pd.concat([rets["A"], rets[k]], axis=1, join="inner").dropna()
                wd[k]["corr_A"] = float(al.corr().iloc[0, 1])
                ci, pb = boot_ci(al.iloc[:, 0].values, al.iloc[:, 1].values)
                wd[k]["sharpe_diff_vs_A"] = sharpe(al.iloc[:, 1].values) - sharpe(al.iloc[:, 0].values)
                wd[k]["sharpe_diff_CI95"] = ci
                wd[k]["boot_P_diff_gt0"] = pb
            mm = pd.concat([mom, rets[k]], axis=1, join="inner").dropna()
            wd[k]["corr_momentum"] = float(mm.corr().iloc[0, 1])
        wd["A"]["corr_A"] = 1.0
        summary["windows"][wname] = wd
    summary["runtime_s"] = round(time.time() - t_start, 1)
    summary["variant_seconds"] = {k: r["seconds"] for k, r in R.items()}
    (OUT / "results.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    reg = ROOT / "data" / "experiments" / "trials_registry.csv"
    bak = reg.with_name(f"trials_registry.backup_{datetime.now():%Y%m%d_%H%M%S}.csv")
    shutil.copy2(reg, bak)
    desc = {"A": "live universe", "B": "all SP500 today (survivorship-biased)",
            "C": "dynamic top100 20d$vol", "D": "dynamic top100 + near252hi5% + sector cap3"}
    with open(reg, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for k in R:
            m = summary["windows"]["full_2011_2025"][k]
            w.writerow([datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "swing_universe", f"swing_d_{k}",
                        hashlib.sha1(f"swing_universe|{k}".encode()).hexdigest()[:10], "sp500_today+live",
                        m["from"], m["to"], round(m["cagr"], 4), round(m["sharpe"], 3), round(-m["max_dd"], 4),
                        m["trades"], f"{desc[k]}; variant d $500k 0.5% risk; turnover col = trade count"])
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
