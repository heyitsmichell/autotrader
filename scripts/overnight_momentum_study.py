#!/usr/bin/env python3
"""Overnight momentum (Lou-Polk-Skouras 2019) vs plain 12-1 on the rotation_lab engine. Holdout (>2025-11-21) never used."""
import sys, time, hashlib, json, csv, shutil, dataclasses
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from app.rotation_lab.sweep import Data, metrics, EVAL_START
from app.rotation_lab.catalog import Panel, load_etfs, REGISTRY, REG_COLS
from app.rotation_lab import momentum_variants as mv
from app.rotation_lab.momentum_variants import Cfg

OUT = ROOT / "data" / "experiments" / "overnight_momentum"
CUT = pd.Timestamp("2025-11-21")
SECT = "XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY".split()


def cut(o, c):
    o, c = o[o.index <= CUT], c[c.index <= CUT]
    assert o.index.max() <= CUT and c.index.max() <= CUT
    return o, c


def rsum(x, win):  # x[t] known at close t; shift(1) => strictly past days t-win..t-1
    return x.shift(1).rolling(win, min_periods=int(win * 0.9)).sum().where(x.shift(1).rolling(win).count() >= int(win * 0.9))


def make_scores(o, c):
    on = np.log(o / c.shift(1)); intra = np.log(c / o)
    on12, on1, id12 = rsum(on, 252), rsum(on, 21), rsum(intra, 252)
    m = mv.mom_12_1(c)
    return {"mom121": m, "overnight_12m": on12, "overnight_1m": on1, "intraday_12m": id12,
            "tug_of_war": mv._rank(on12) - mv._rank(id12), "overnight_x_mom": mv.rankavg(on12, m)}


def cfgs(u, N):
    p = f"{u}|N{N}|"
    L = [Cfg(p + "plain_12-1", "plain", u, N, "mom121", note="benchmark")]
    for s in ("overnight_12m", "overnight_1m", "intraday_12m", "tug_of_war", "overnight_x_mom"):
        L.append(Cfg(p + s, "plain", u, N, s))
    L.append(Cfg(p + "overnight_12m_weekly", "plain", u, N, "overnight_12m", rebal="weekly"))
    return L


def chash(c): return hashlib.md5(json.dumps(dataclasses.asdict(c), sort_keys=True).encode()).hexdigest()[:10]


def run_one(P, cols, c, scores):
    top = mv.top_idx
    S = scores[c.score][cols].values
    def row(t):
        ix = top(S[t], c.N)
        return {cols[i]: 1.0 / c.N for i in ix} if len(ix) else None
    from app.rotation_lab.catalog import build_rebal, evaluate
    W, flag = build_rebal(P, row, "weekly" if c.rebal == "weekly" else "monthly")
    return evaluate(P, W, flag)


def main():
    t0 = time.time(); OUT.mkdir(parents=True, exist_ok=True)
    d = Data(); rows, rets = [], {}
    def do(P, u, cols, cl):
        sc = make_scores(P.o[cols], P.c[cols])
        spy_r, _ = mv.run_benchmark(P, "spy", cols); ew_r, ew_t = mv.run_benchmark(P, "ew", cols)
        for nm, (r, t) in {f"{u}|SPY_buyhold": mv.run_benchmark(P, "spy", cols), f"{u}|EW_universe": (ew_r, ew_t)}.items():
            rets[nm] = r
            rows.append(dict(name=nm, kind="benchmark", universe=u, N=0, start="2011-01-03", **metrics(r, t, spy_r), **mv.extra_metrics(r[r.index >= EVAL_START])))
        for c in cl:
            r, t = run_one(P, cols, c, sc); rets[c.name] = r
            st = pd.Timestamp(c.start)
            rows.append(dict(name=c.name, kind="variant", universe=u, N=c.N, score=c.score, rebal=c.rebal, start=c.start, config_hash=chash(c),
                             note=c.note, **metrics(r[r.index >= st], t[t.index >= st], spy_r[spy_r.index >= st]), **mv.extra_metrics(r[r.index >= st])))
            print(c.name, f"sharpe {rows[-1]['sharpe']:.2f} cagr {rows[-1]['cagr']:.3f}", flush=True)
    for u, cols in (("app87", [x for x in d.univ["app87"] if x not in ("SPY", "QQQ", "TLT", "IEF", "BIL")]), ("sp500", d.univ["sp500"])):
        ex = cols + [x for x in ("SPY", "BIL") if x not in cols]
        o, c = cut(d.open[ex], d.close[ex])
        for N in (10, 5):
            do(Panel(o, c), u, cols, cfgs(u, N)) if N == 10 else None
        # N=5 reuses panel; run separately to keep benchmarks single
        P = Panel(o, c); sc = make_scores(P.o[cols], P.c[cols]); spy_r = rets[f"{u}|SPY_buyhold"]
        for c5 in cfgs(u, 5):
            r, t = run_one(P, cols, c5, sc); rets[c5.name] = r
            rows.append(dict(name=c5.name, kind="variant", universe=u, N=5, score=c5.score, rebal=c5.rebal, start=c5.start, config_hash=chash(c5),
                             note=c5.note, **metrics(r, t, spy_r), **mv.extra_metrics(r)))
    P0 = load_etfs(SECT); o, c = cut(P0.o, P0.c); P = Panel(o, c)
    do(P, "sector", [s for s in SECT if s in P.c.columns], cfgs("sector", 3))
    pd.DataFrame(rows).to_csv(OUT / "leaderboard.csv", index=False)
    pd.DataFrame(rets).to_csv(OUT / "daily_returns.csv.gz")
    print("done", time.time() - t0)
    finalize()


def finalize():
    lb = pd.read_csv(OUT / "leaderboard.csv")
    R = pd.read_csv(OUT / "daily_returns.csv.gz", index_col=0, parse_dates=True)
    assert R.index.max() <= CUT
    var = lb[lb.kind == "variant"]
    nreg, nmine = mv.n_registry_rows(), len(var)
    sl = lambda n, st: R[n][R.index >= pd.Timestamp(st)].dropna()
    srs = np.array([sl(r["name"], r["start"]).mean() / sl(r["name"], r["start"]).std() for _, r in var.iterrows()])
    srvar = max(float(np.nanvar(srs)), 1.728e-4)
    dsrs, ci = {}, {}
    for _, r in var.iterrows():
        x = sl(r["name"], r["start"])
        dsrs[r["name"]] = mv.dsr(x, nreg, nmine, srvar)
        base = f"{r['universe']}|N{int(r['N'])}|plain_12-1"
        ci[r["name"]] = mv.block_boot_dsharpe(x, sl(base, r["start"])) if r["name"] != base else (0.0, 0.0, 0.0)
    lb["dsr"] = lb.name.map(dsrs); lb["n_trials_total"] = nreg + nmine
    for i, k in enumerate(("dsharpe_vs_plain", "dsharpe_ci_lo", "dsharpe_ci_hi")):
        lb[k] = lb.name.map(lambda n: ci.get(n, (np.nan,) * 3)[i])
    lb["ci_excludes_0"] = (lb.dsharpe_ci_lo > 0) | (lb.dsharpe_ci_hi < 0)
    lb.to_csv(OUT / "leaderboard.csv", index=False)
    ex = pd.read_csv(REGISTRY, on_bad_lines="skip")
    if (ex["lab"] == "overnight_momentum").any():
        print("registry already has rows"); return
    shutil.copy(REGISTRY, REGISTRY.with_name(f"trials_registry.backup_{time.strftime('%Y%m%d_%H%M%S')}.csv"))
    with open(REGISTRY, "a", newline="", encoding="utf8") as f:
        w = csv.DictWriter(f, fieldnames=REG_COLS)
        for _, r in lb[lb.kind == "variant"].iterrows():
            w.writerow(dict(ts=pd.Timestamp.now().isoformat(timespec="seconds"), lab="overnight_momentum", strategy=r["name"],
                            config_hash=r["config_hash"], universe=r["universe"], start=r["start"], end="2025-11-21",
                            cagr=round(r["cagr"], 4), sharpe=round(r["sharpe"], 3), maxdd=round(r["maxdd"], 4), turnover=round(r["turnover_yr"], 2),
                            notes=("biased univ; " if r["universe"] != "sector" else "survivorship-free; ") + str(r["note"])))
    print("finalised", nreg, nmine)


if __name__ == "__main__":
    main()
