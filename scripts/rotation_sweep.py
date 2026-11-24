#!/usr/bin/env python3
"""Run source strategies A-D, benchmarks and a random ~300-config sweep. Writes leaderboard + REPORT."""
import sys, time, dataclasses
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from app.rotation_lab.sweep import (Data, run_static, run_spec, metrics, sample_specs, SOURCE, deflated_sharpe, EVAL_START)

OUT = ROOT / "data" / "experiments" / "rotation"


def fmt(x, cols):
    return "```\n" + x[cols].round(3).to_string(index=False) + "\n```"


def main(n=300):
    t0 = time.time(); OUT.mkdir(parents=True, exist_ok=True)
    d = Data()
    print("data", len(d.syms), len(d.index), d.index[0].date(), d.index[-1].date(), "sp500", len(d.univ["sp500"]), "app87", len(d.univ["app87"]))
    bench = {"SPY_buyhold": run_static(d, {"SPY": 1.0}), "60_40_SPY_IEF": run_static(d, {"SPY": .6, "IEF": .4}),
             "EW_sectors": run_static(d, d.univ["sector"])}
    spy = bench["SPY_buyhold"][0]
    rows = []; rets = {}
    for k, (r, t) in bench.items():
        rows.append(dict(name=k, kind="benchmark", **metrics(r, t, spy))); rets[k] = r
    for sp in SOURCE + sample_specs(n):
        r, t = run_spec(d, sp); rets[sp.name] = r
        kind = "source" if sp.name[:2] in ("A_", "B_", "C_", "D_") else "sweep"
        rows.append(dict(kind=kind, **dataclasses.asdict(sp), **metrics(r, t, spy)))
    df = pd.DataFrame(rows)
    trial_names = set(df[df.kind != "benchmark"].name)
    ev = lambda k: rets[k][rets[k].index >= EVAL_START]
    srs = np.array([ev(k).mean() / ev(k).std() for k in trial_names])
    srvar = float(np.nanvar(srs)); N = len(trial_names)
    df["dsr"] = [deflated_sharpe(ev(k), N, srvar) if k in trial_names else np.nan for k in df.name]
    sharpe_spy = df.loc[df.name == "SPY_buyhold", "sharpe"].iloc[0]
    df["dsr_gt_95"] = df.dsr > 0.95
    df["beats_spy_sharpe"] = df.sharpe > sharpe_spy
    df = df.sort_values("dsr", ascending=False)
    df.to_csv(OUT / "leaderboard.csv", index=False)
    pd.DataFrame(rets).to_csv(OUT / "daily_returns.csv.gz")
    rt = time.time() - t0
    cols = ["name", "cagr", "vol", "sharpe", "sortino", "maxdd", "mar", "turnover_yr", "pct_years_beat_spy", "blocks_beating_spy_sr", "block_sharpes", "dsr"]
    star = df[df.kind != "benchmark"]
    good = star[(star.dsr > 0.95) & star.beats_spy_sharpe]
    rep = f"""# Rotation lab report
Metrics window {EVAL_START.date()} .. {d.index[-1].date()} (data from 2010-01; 252d warmup). Holdout 2025-11-24+ never loaded.
Trials N={N}; variance of trial daily Sharpes {srvar:.3e}; runtime {rt:.0f}s. Costs: 5bps slippage/side, SEC 2.78e-5 on sells, TAF 0.000166/sh.
block_sharpes = annualised Sharpe in 2011-13, 14-16, 17-19, 20-22, 23-25.

## Benchmarks and source strategies A-D
{fmt(df[df.kind != 'sweep'].sort_values('name'), cols)}

## Top 15 by deflated Sharpe
{fmt(star.head(15), cols)}

## Configs with DSR>0.95 and Sharpe > SPY: {len(good)}
{fmt(good.head(30), cols) if len(good) else 'none'}

Caveats: current S&P 500 constituents (survivorship bias, large for stock universes); configs are correlated so effective trials < N; momentum crashes (2009 outside window, 2020 inside); A's vol-capping (inverse-vol weights capped at 2/N) and 'daily' (rebalance when the selected set changes) are my interpretations; app87 universe is selected with hindsight.
"""
    (OUT / "REPORT.md").write_text(rep, encoding="utf8")
    print(rep); print("runtime", rt)


def catalog():
    from app.rotation_lab.catalog import run_catalog
    df, N, srvar, skipped, rt = run_catalog(OUT)
    cols = ["name", "cagr", "vol", "sharpe", "sortino", "maxdd", "mar", "turnover_yr", "pct_years_beat_spy", "blocks_beating_spy_sr", "block_sharpes", "dsr"]
    star = df[df.group != "benchmark"]
    b = df[df.group == "benchmark"]
    mf = star[star.group == "metals_fx"]
    spy_sr = float(b.loc[b.name == "SPY_buyhold", "sharpe"].iloc[0])
    pub = star[star.published].sort_values("dsr", ascending=False)
    rep = f"""# Catalog report (published strategies + neighbourhoods)
Window {EVAL_START.date()}..2025-11-21 (holdout never loaded). Trials this run {len(star)}; TOTAL trials for deflation N={N} (registry + 304 prior); trial Sharpe variance {srvar:.3e}; runtime {rt:.0f}s.
Costs: 5bps slippage/side, SEC fee, TAF. Signals at close t, trade next open. Long-only, no leverage; long/short factors are long leg only. Survivorship bias in stock rows (today's S&P 500).
Skipped/errored: {skipped if skipped else 'none'}

## Benchmarks
{fmt(b, cols)}

## Top 10 by deflated Sharpe (all trials)
{fmt(star.sort_values('dsr', ascending=False).head(10), cols)}

## Published (as-is) versions, by DSR
{fmt(pub, cols)}

## Metals and currencies (separate)
{fmt(mf.sort_values('dsr', ascending=False), cols)}

SPY Sharpe {spy_sr:.3f}. Rows with DSR>0.95 and Sharpe>SPY: {int(((star.dsr > 0.95) & (star.sharpe > spy_sr)).sum())}.
"""
    (OUT / "REPORT_CATALOG.md").write_text(rep, encoding="utf8")
    print(rep)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "catalog":
        catalog()
    else:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
