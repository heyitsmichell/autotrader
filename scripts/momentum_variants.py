#!/usr/bin/env python3
"""Run the 12 momentum variants x universes. Saves leaderboard/returns after every universe; finalises DSR + bootstrap."""
import sys, time, hashlib, json, csv, dataclasses
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from app.rotation_lab.sweep import Data, metrics, RESEARCH_END, EVAL_START
from app.rotation_lab.catalog import Panel, load_etfs, REGISTRY, REG_COLS
from app.rotation_lab import momentum_variants as mv
from app.rotation_lab.momentum_variants import Cfg

OUT = ROOT / "data" / "experiments" / "momentum_variants"
G12 = ["SPY", "IWM", "QQQ", "VGK", "EWJ", "VWO", "VNQ", "DBC", "GLD", "TLT", "HYG", "LQD"]
SECT = "XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY".split()
SEAS_START = "2016-01-04"


def stock_cfgs(u, N=10):
    p = f"{u}|N{N}|"
    L = [Cfg(p + "plain_12-1", "plain", u, N, "mom121", note="benchmark"),
         Cfg(p + "volscale_cap1.0", "volscale", u, N, cap=1.0, note="Barroso-SC 12% target"),
         Cfg(p + "volscale_cap1.5", "volscale", u, N, cap=1.5, note="LEVERAGE; borrow at BIL, no spread"),
         Cfg(p + "dmom_crash", "dmom", u, N, note="DM long-only; first signal ~2012"),
         Cfg(p + "breadth40", "breadth", u, N, note="IEF if <40% above SMA200")]
    if N == 10:
        L += [Cfg(p + "fip_top2N", "fip", u, N, note="Da-Gurun-Warachka"),
              Cfg(p + "qm_gray_vogel", "qm", u, N, rebal="quarterly", note="top decile -> best half FIP; Feb/May/Aug/Nov"),
              Cfg(p + "mom_12-7", "plain", u, N, "mom127"),
              Cfg(p + "mom_12-7+6-1", "plain", u, N, "mom127_61"),
              Cfg(p + "seasonal", "plain", u, N, "seasonal", start=SEAS_START, note="Heston-Sadka; starts 2016"),
              Cfg(p + "accel", "plain", u, N, "accel"),
              Cfg(p + "accel+12-1", "plain", u, N, "accel_121"),
              Cfg(p + "mom_12-0", "plain", u, N, "mom120", note="no skip"),
              Cfg(p + "mom_skip1w", "plain", u, N, "mom_skip1w"),
              Cfg(p + "clenow", "clenow", u, N, rebal="weekly", note="Clenow SotM; ATR=close-to-close proxy"),
              Cfg(p + "mom+lowvol", "plain", u, N, "mom_lowvol")]
    return L


def etf_cfgs(u):
    p = f"{u}|N3|"
    L = [Cfg(p + "plain_12-1", "plain", u, 3, "mom121", note="benchmark"),
         Cfg(p + "volscale_cap1.0", "volscale", u, 3, cap=1.0),
         Cfg(p + "dmom_crash", "dmom", u, 3),
         Cfg(p + "mom_12-7", "plain", u, 3, "mom127"),
         Cfg(p + "accel", "plain", u, 3, "accel"),
         Cfg(p + "mom+lowvol", "plain", u, 3, "mom_lowvol")]
    if u == "sector":
        L.insert(1, Cfg(p + "sector_mom_6m", "plain", u, 3, "mom6", note="Moskowitz-Grinblatt (6m)"))
        L.append(Cfg(p + "seasonal", "plain", u, 3, "seasonal", start=SEAS_START, note="starts 2016"))
    return L


def chash(c): return hashlib.md5(json.dumps(dataclasses.asdict(c), sort_keys=True).encode()).hexdigest()[:10]


def all_cfgs():
    a = stock_cfgs("app87", 10) + [c for c in stock_cfgs("app87", 5) if c.variant in ("plain", "volscale", "dmom", "breadth") and "cap1.5" not in c.name]
    return a, stock_cfgs("sp500", 10), etf_cfgs("sector"), etf_cfgs("g12")


def basic_row(c, r, t, spy):
    st = pd.Timestamp(c.start)
    r = r[r.index >= st]; t = t[t.index >= st]; s = spy[spy.index >= st]
    return dict(name=c.name, kind="variant", universe=c.universe, N=c.N, variant=c.variant, score=c.score, rebal=c.rebal,
                start=c.start, config_hash=chash(c), note=c.note, **metrics(r, t, s), **mv.extra_metrics(r))


def main():
    t0 = time.time(); OUT.mkdir(parents=True, exist_ok=True)
    d = Data()
    rows, rets = [], {}

    def save():
        pd.DataFrame(rows).to_csv(OUT / "leaderboard.csv", index=False)
        pd.DataFrame(rets).to_csv(OUT / "daily_returns.csv.gz")
        print(f"saved {len(rows)} rows, {time.time() - t0:.0f}s", flush=True)

    def do_universe(P, u, cols, cfgs):
        ctx = mv.Ctx(P, cols)
        spy_r, spy_t = mv.run_benchmark(P, "spy", cols)
        ew_r, ew_t = mv.run_benchmark(P, "ew", cols)
        for nm, (r, t) in {f"{u}|SPY_buyhold": (spy_r, spy_t), f"{u}|EW_universe": (ew_r, ew_t)}.items():
            rets[nm] = r
            rows.append(dict(name=nm, kind="benchmark", universe=u, N=0, start="2011-01-03", **metrics(r, t, spy_r),
                             **mv.extra_metrics(r[r.index >= EVAL_START])))
        plain = {}
        for c in cfgs:
            ru = None
            if c.variant == "volscale":
                if c.N not in plain:
                    plain[c.N] = mv.run_cfg(Cfg("x", "plain", u, c.N, "mom121"), ctx, P)[0]
                ru = plain[c.N]
            r, t = mv.run_cfg(c, ctx, P, ru)
            rets[c.name] = r
            rows.append(basic_row(c, r, t, spy_r))
            print(c.name, f"sharpe {rows[-1]['sharpe']:.2f} cagr {rows[-1]['cagr']:.3f}", flush=True)
        save()

    a87, s500, sec, g12 = all_cfgs()
    for u, cols, cf in (("app87", [x for x in d.univ["app87"] if x not in ("SPY", "QQQ", "TLT", "IEF", "BIL")], a87), ("sp500", d.univ["sp500"], s500)):
        ex = cols + [x for x in ("SPY", "BIL", "IEF") if x not in cols]
        do_universe(Panel(d.open[ex], d.close[ex]), u, cols, cf)
    for u, cols, cf in (("sector", SECT, sec), ("g12", G12, g12)):
        P = load_etfs(cols + ["IEF"])
        do_universe(P, u, [s for s in cols if s in P.c.columns], cf)
    finalize(False)


def finalize(registry=True):
    lb = pd.read_csv(OUT / "leaderboard.csv")
    R = pd.read_csv(OUT / "daily_returns.csv.gz", index_col=0, parse_dates=True)
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
    lb["dsr"] = lb.name.map(dsrs)
    lb["n_trials_total"] = nreg + nmine
    for i, k in enumerate(("dsharpe_vs_plain", "dsharpe_ci_lo", "dsharpe_ci_hi")):
        lb[k] = lb.name.map(lambda n: ci.get(n, (np.nan,) * 3)[i])
    lb["ci_excludes_0"] = (lb.dsharpe_ci_lo > 0) | (lb.dsharpe_ci_hi < 0)
    lb.to_csv(OUT / "leaderboard.csv", index=False)
    if not registry:
        return
    existing = pd.read_csv(REGISTRY, on_bad_lines="skip") if REGISTRY.exists() else pd.DataFrame(columns=REG_COLS)
    if (existing.get("lab", pd.Series(dtype=str)) == "momentum_variants").any():
        print("registry already has momentum_variants rows; not appending"); return
    with open(REGISTRY, "a", newline="", encoding="utf8") as f:
        w = csv.DictWriter(f, fieldnames=REG_COLS)
        for _, r in lb[lb.kind == "variant"].iterrows():
            w.writerow(dict(ts=pd.Timestamp.now().isoformat(timespec="seconds"), lab="momentum_variants", strategy=r["name"],
                            config_hash=r["config_hash"], universe=r["universe"], start=r["start"], end=str(RESEARCH_END.date()),
                            cagr=round(r["cagr"], 4), sharpe=round(r["sharpe"], 3), maxdd=round(r["maxdd"], 4),
                            turnover=round(r["turnover_yr"], 2),
                            notes=("biased univ; " if r["universe"] in ("app87", "sp500") else "survivorship-free; ") + str(r["note"])))
    print("finalised", nreg, nmine)


if __name__ == "__main__":
    finalize() if len(sys.argv) > 1 and sys.argv[1] == "finalize" else main()
