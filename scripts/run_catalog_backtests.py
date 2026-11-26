#!/usr/bin/env python3
"""Run master_strategy_catalog.csv backtests (research only, data <= 2025-11-21). Writes data/experiments/catalog/*.
Usage: python scripts/run_catalog_backtests.py [--only name,name] [--nolog] [--maxcfg 20]"""
from __future__ import annotations
import argparse, itertools, json, random, sys, time
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.catalog_lab import data as D                      # noqa: E402
from app.catalog_lab.strategies import STRATS, NOT_TESTABLE, Ctx   # noqa: E402

REFS = {"momentum": "momentum", "swing": "swing", "vaa": "vaa", "putwrite": "putwrite"}


def sample_grid(grid: dict, cap: int, seed=7):
    keys = list(grid)
    combos = [dict(zip(keys, v)) for v in itertools.product(*grid.values())]
    n_full = len(combos)
    if n_full > cap:
        combos = [combos[i] for i in sorted(random.Random(seed).sample(range(n_full), cap))]
    return combos, n_full


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--nolog", action="store_true")
    ap.add_argument("--maxcfg", type=int, default=20)
    a = ap.parse_args()
    only = [x for x in a.only.split(",") if x]
    t0 = time.time()
    P = D.Panel()
    ctx = Ctx(P)
    idx = P.index
    print(f"panel {len(P.stocks)} stocks x {len(idx)} days {idx[0].date()}..{idx[-1].date()}", flush=True)

    refs = {}
    for k in REFS:
        r = pd.read_csv(ROOT / "app" / "sleeves" / "reference" / f"{k}.csv.gz", parse_dates=["date"]).set_index("date")["ret"]
        refs[k] = r[r.index <= D.END]
    # SPY buy&hold benchmark (same open-to-open convention, same costs)
    g, n, t = D.sim(np.ones((len(idx), 1)), P.spy["open"].values[:, None])
    spy = pd.Series(n, index=idx)
    bench = {}
    for lab, st in (("full", D.START), ("3y", D.L3Y)):
        bench[lab] = D.metrics(spy[spy.index > st] if lab == "3y" else spy[spy.index >= st])

    rows, series = [], {}
    for name, S in STRATS.items():
        if only and name not in only:
            continue
        cfgs, n_full = sample_grid(S["grid"], a.maxcfg)
        if "collapse" in S:
            pass
        t1 = time.time()
        outs = [None] * len(cfgs)
        if S.get("multi"):
            groups = {}
            for i, c in enumerate(cfgs):
                groups.setdefault((c["pca_window_days"], c["num_eigenvectors"]), []).append(i)
            for ids in groups.values():
                res = S["multi"](ctx, [cfgs[i] for i in ids])
                for i, o in zip(ids, res):
                    outs[i] = o
        else:
            for i, c in enumerate(cfgs):
                outs[i] = S["fn"](ctx, c)
        for c, o in zip(cfgs, outs):
            net = pd.Series(o["net"], index=idx); gro = pd.Series(o["gross"], index=idx); tn = pd.Series(o["turn"], index=idx)
            nz = np.where(o["turn"] > 0)[0]
            st = max(D.START, idx[nz[0]]) if len(nz) else D.START
            nsl, gsl = net[net.index >= st], gro[gro.index >= st]
            m, mg = D.metrics(nsl, tn), D.metrics(gsl)
            n3 = net[net.index > D.L3Y]
            m3 = D.metrics(n3, tn)
            row = dict(strategy=name, config=json.dumps(c, sort_keys=True), start=str(st.date()), end=str(D.END.date()),
                       gross_cagr=mg["cagr"], gross_sharpe=mg["sharpe"], cagr=m["cagr"], sharpe=m["sharpe"],
                       maxdd=m["maxdd"], turnover=m["turnover"], l3y_cagr=m3["cagr"], l3y_sharpe=m3["sharpe"],
                       l3y_maxdd=m3["maxdd"], n_days=len(nsl))
            for k, rr in refs.items():
                j = pd.concat([n3, rr], axis=1, join="inner").dropna()
                row[f"corr_{k}"] = float(j.iloc[:, 0].corr(j.iloc[:, 1])) if len(j) > 100 and j.iloc[:, 0].std() > 0 else np.nan
            rows.append(row)
            series[(name, row["config"])] = net
        print(f"{name}: {len(cfgs)}/{n_full} cfgs in {time.time() - t1:.0f}s", flush=True)
        outs = None
    df = pd.DataFrame(rows)
    df.to_csv(D.OUT / "results.csv", index=False)

    # best / median series
    keep = {}
    for name, d in df.groupby("strategy"):
        d = d.sort_values("sharpe")
        for lab, r in (("best", d.iloc[-1]), ("median", d.iloc[len(d) // 2])):
            keep[f"{name}|{lab}"] = series[(name, r["config"])]
    pd.DataFrame(keep).to_csv(D.OUT / "daily_best_median.csv.gz")

    if not a.nolog:
        for r in rows:
            D.log_trial(r["strategy"], r["config"], "sp500_today" if STRATS[r["strategy"]]["data"].startswith(("S&P", "100", "top", "18"))
                        else "etf/index", r["start"], r["end"],
                        dict(cagr=r["cagr"], sharpe=r["sharpe"], maxdd=r["maxdd"], turnover=r["turnover"]),
                        "NET; " + ("survivorship-biased univ; " if "S&P" in STRATS[r["strategy"]]["data"] or "S&P" in STRATS[r["strategy"]]["caveats"] else "")
                        + ("modelled premiums, not real fills" if r["strategy"] == "iv_rank_iron_condor_harvest" else ""))
    write_report(df, bench)
    print(f"done in {time.time() - t0:.0f}s; rows={len(df)}", flush=True)


def f(x, pct=False):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x * 100:.1f}%" if pct else f"{x:.2f}"


def write_report(df, bench):
    L = ["# Catalog backtest report", "",
         "Research only. Data strictly <= 2025-11-21 (locked holdout untouched). Timing: signal at close t, trade at open t+1, open-to-open returns "
         "(rotation_lab convention). Costs: 5 bps/side + SEC fee on sells (rotation_lab model); gross also in results.csv. Cash earns 0 unless stated. "
         "All equity-universe results use today's S&P 500 list: **survivorship-biased**. Configs per strategy capped at 20 (seeded random sample of the catalog grid); "
         "every config is in results.csv and logged to data/experiments/trials_registry.csv with lab=catalog. 'Best' = highest net Sharpe of the sampled configs (selection bias: "
         "treat the MEDIAN as the honest number). Correlations = daily net returns vs app/sleeves/reference series over 2022-11-22..2025-11-21 for the best config.", "",
         f"SPY buy&hold same convention: full(2011-01..) CAGR {f(bench['full']['cagr'], 1)} Sharpe {f(bench['full']['sharpe'])} MaxDD {f(bench['full']['maxdd'], 1)}; "
         f"last 3y CAGR {f(bench['3y']['cagr'], 1)} Sharpe {f(bench['3y']['sharpe'])} MaxDD {f(bench['3y']['maxdd'], 1)}. "
         "Live reference: swing variant d 23.9% CAGR / Sharpe 1.05 / MaxDD -24.8%.", "",
         "| strategy | testable | data | configs (run/grid) | best net CAGR / Sharpe / MaxDD | median cfg net CAGR / Sharpe / MaxDD | last-3y Sharpe (best / median) | corr mom/swing/vaa/putwrite (best) | caveats |",
         "|---|---|---|---|---|---|---|---|---|"]
    secs = []
    for name, S in STRATS.items():
        d = df[df.strategy == name].sort_values("sharpe") if len(df) else df
        if not len(d):
            continue
        b, m = d.iloc[-1], d.iloc[len(d) // 2]
        full = int(np.prod([len(v) for v in S["grid"].values()]))
        cor = "/".join(f(b[f"corr_{k}"]) for k in REFS)
        L.append(f"| {name} | yes | {S['data']} | {len(d)}/{full} | {f(b.cagr, 1)} / {f(b.sharpe)} / {f(b.maxdd, 1)} | "
                 f"{f(m.cagr, 1)} / {f(m.sharpe)} / {f(m.maxdd, 1)} | {f(b.l3y_sharpe)} / {f(m.l3y_sharpe)} | {cor} | {S['caveats'][:140]}... |")
        secs.append(f"### {name}\n- Data: {S['data']}. Configs run: {len(d)} of {full} grid points. Period {b.start}..{b.end}.\n"
                    f"- Best (net): {b.config} -> CAGR {f(b.cagr, 1)}, Sharpe {f(b.sharpe)}, MaxDD {f(b.maxdd, 1)}, turnover {b.turnover:.1f}x/yr; gross CAGR {f(b.gross_cagr, 1)} / Sharpe {f(b.gross_sharpe)}.\n"
                    f"- Median config: {m.config} -> CAGR {f(m.cagr, 1)}, Sharpe {f(m.sharpe)}, MaxDD {f(m.maxdd, 1)}. Share of configs with net Sharpe>0.5: {(d.sharpe > 0.5).mean():.0%}; worst Sharpe {f(d.sharpe.min())}.\n"
                    f"- Last 3y (best / median): CAGR {f(b.l3y_cagr, 1)} / {f(m.l3y_cagr, 1)}, Sharpe {f(b.l3y_sharpe)} / {f(m.l3y_sharpe)}, MaxDD {f(b.l3y_maxdd, 1)} / {f(m.l3y_maxdd, 1)}.\n"
                    f"- Caveats / simplifications: {S['caveats']}.\n")
    for name, why in NOT_TESTABLE.items():
        L.append(f"| {name} | NOT TESTABLE | - | 0 | - | - | - | - | {why[:140]}... |")
        secs.append(f"### {name}\n- NOT TESTABLE. {why}\n")
    L += ["", "## Per-strategy notes", ""] + secs
    sp = D.OUT / "SHORTLIST.md"
    if sp.exists():
        L += ["", sp.read_text(encoding="utf-8")]
    (D.OUT / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
