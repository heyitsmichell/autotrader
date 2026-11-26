#!/usr/bin/env python3
"""Strategy experiment CLI (research only).
  python scripts/experiments.py --list
  python scripts/experiments.py --run all            # research set only
  python scripts/experiments.py --holdout d_champion,momentum_d   # explicit, logged peek
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _job(a):
    from app.strategy_lab import harness
    return harness.run_variant(a[0], a[1], n_trials=a[2])


def _jobj(v):
    from app.strategy_lab import harness
    return harness.run_variant(v, "research", n_trials=0)


def _score(r, n_trials):
    from app.strategy_lab import harness
    m, se, pen = harness.penalized_score(r["trades"], r["r_sum"], r["r_sq"], n_trials)
    return {**r, "exp_R": m, "se": se, "penalized": pen}


def boot_ci(trade_rs, n=2000, seed=0):
    """Block bootstrap by entry month: resample months with replacement, 90% CI of pooled mean R."""
    import numpy as np
    by = {}
    for mth, r in trade_rs:
        by.setdefault(mth, []).append(r)
    ms = [(sum(v), len(v)) for v in by.values()]
    rng = np.random.default_rng(seed)
    k = len(ms)
    arr = np.array(ms, dtype=float)
    out = []
    for _ in range(n):
        ix = rng.integers(0, k, k)
        t = arr[ix].sum(axis=0)
        out.append(t[0] / t[1] if t[1] else 0.0)
    return float(np.percentile(out, 5)), float(np.percentile(out, 95))


def search(workers: int) -> int:
    """Staged search (research only, never the holdout). Penalty uses TOTAL trials across stages."""
    from app.strategy_lab import families, harness, variants
    t0 = time.time()
    pool = Pool(workers)
    allres: dict = {}

    def run(vs):
        vs = [v for v in vs if v.name not in allres]
        for v, r in zip(vs, pool.map(_jobj, vs)):
            allres[v.name] = (v, r)
        return [allres[v.name] for v in vs]

    def top(k, pool_names=None, min_trades=100):
        cand = [(v, r) for n, (v, r) in allres.items() if (pool_names is None or n in pool_names)
                and r["trades"] >= min_trades]
        cand = [(v, _score(r, len(allres))) for v, r in cand]
        cand.sort(key=lambda x: -x[1]["penalized"])
        return cand[:k]

    champ = variants.REGISTRY[variants.CHAMPION]
    fams = ["consensus", "pullback", "breakout20_vol", "momentum", "meanrev", *families.NEW_FAMILIES]
    s1 = [variants.make(f, "d", rg, 10) for f in fams for rg in families.REGIMES]
    run([champ, variants.make("consensus", "d", "none", 10)] + s1)
    s1names = {v.name for v in s1}
    print(f"stage1 done {len(allres)} trials {time.time()-t0:.0f}s", flush=True)
    t6 = top(6, s1names)
    s2 = []
    for v, _ in t6:
        styles = ["d_trail3", "d_time10", "d_time40", "d_sl1.5", "d_sl2.5", "d_sl3", "chand3"]
        if v.entry in families.NATIVE_EXIT_FAMILIES:
            styles.append("native")
        s2 += [variants.make(v.entry, e, v.regime, 10) for e in styles]
    run(s2)
    s2names = {v.name for v in s2}
    print(f"stage2 done {len(allres)} trials {time.time()-t0:.0f}s", flush=True)
    t6b = top(6, s1names | s2names)
    s3 = [variants.make(v.entry, v.exit_style, v.regime, mp, ml) for v, _ in t6b
          for mp in (5, 10, 20) for ml in (False, True)]
    run(s3)
    print(f"stage3 done {len(allres)} trials {time.time()-t0:.0f}s", flush=True)
    ntr = len(allres)
    scored = [(v, _score(r, ntr)) for v, r in allres.values()]
    rows = sorted([x for x in scored if x[1]["trades"] >= 100 and x[0].name != variants.CHAMPION],
                  key=lambda x: -x[1]["penalized"])
    cres = next(r for v, r in scored if v.name == variants.CHAMPION)
    cut, dates = harness.data_cutoff(harness.ROOT / "data" / "bars_yahoo")
    rd, _, _ = harness.split_dates(dates)
    a, b = harness.make_folds(rd)[0][1], rd[-1]
    bench = harness.benchmark_perf(a, b, harness.research_dir(harness.ROOT / "data" / "bars_yahoo", cut))
    bench["d_champion"] = cres["perf"]
    out = {"n_trials": ntr, "window": [str(a), str(b)], "benchmarks": bench, "top": [], "seconds": time.time() - t0}

    def fmt(v, r):
        lo, hi = boot_ci(r["trade_rs"])
        p = r["perf"]
        signs = "".join("+" if f["exp_R"] > 0 else "-" for f in r["folds"])
        return {"name": v.name, "trades": r["trades"], "expR": r["exp_R"], "pen": r["penalized"], "ci90": [lo, hi],
                "folds": signs, **p}
    for v, r in rows[:10]:
        out["top"].append(fmt(v, r))
    out["champion"] = fmt(champ, next(r for v, r in scored if v.name == variants.CHAMPION))
    out["all"] = [{"name": v.name, "trades": r["trades"], "expR": r["exp_R"], "pen": r["penalized"],
                   "mar": r["perf"]["mar"], "sharpe": r["perf"]["sharpe"], "cagr": r["perf"]["cagr"],
                   "max_dd": r["perf"]["max_dd"]} for v, r in scored]
    (harness.EXP_DIR / f"search_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ("n_trials", "window", "benchmarks", "top", "champion", "seconds")}, indent=1))
    return 0


def main() -> int:
    from app.strategy_lab import harness, leaderboard, variants
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", default="")
    ap.add_argument("--holdout", default="", help="LOCKED holdout (last 365d); each use is logged")
    ap.add_argument("--search", action="store_true", help="staged wide search (research only)")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--ta-v3", action="store_true", help="TA v3 vs v2 evaluation (research only)")
    a = ap.parse_args()
    if a.ta_v3:
        from app.strategy_lab import ta_v3_eval
        out = ta_v3_eval.run_all(max(a.workers, 1))
        print(json.dumps({"matched_min": out["calibration"]["matched_min"],
                          "seconds": out["seconds"]}, indent=1))
        return 0
    if a.list:
        for v in variants.REGISTRY.values():
            print(f"{v.name:<24} entry={v.entry:<15} exit={v.exit_style:<8} ml={v.ml_filter!s:<5} "
                  f"{v.config_hash()}  {v.description}")
        return 0
    if a.search:
        return search(a.workers)
    t0 = time.time()
    scope, names = ("holdout", a.holdout) if a.holdout else ("research", a.run)
    if not names:
        ap.print_help()
        return 1
    vs = variants.get(names)
    if scope == "holdout":
        if variants.CHAMPION not in [v.name for v in vs]:
            vs = [variants.REGISTRY[variants.CHAMPION]] + vs   # promotion needs the champion's holdout
        # run_variant(scope="holdout") logs each peek itself (prior count in result["prior_holdout_peeks"])
        print(f"HOLDOUT run for: {', '.join(v.name for v in vs)} (each peek is logged by run_variant)")
    ntr = max(len(variants.REGISTRY), len(leaderboard.load().config_hash.unique()))
    jobs = [(v.name, scope, ntr) for v in vs]
    results = []
    it = Pool(a.workers).imap(_job, jobs) if a.workers > 1 else map(_job, jobs)
    for r in it:
        print(f"{r['variant']:<24} {r['seconds']:>6}s trades={r['trades']} exp={r['exp_R']:+.3f}R "
              f"pen={r['penalized']:+.3f}", flush=True)
        results.append(r)
    leaderboard.append(results)
    out = harness.EXP_DIR / "runs"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{scope}_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(json.dumps(results, indent=1, default=str))
    if scope == "research":
        df = leaderboard.rank(leaderboard.load())
        print(leaderboard.table(df, variants.CHAMPION))
        champ = df[df.variant == variants.CHAMPION]
        if len(champ):
            c = champ.iloc[0].to_dict()
            c["fold_exp_R"] = json.loads(c["fold_exp_R"])
            print(f"\nchampion penalized={c['penalized']:+.3f} folds={c['fold_exp_R']}")
            for _, r in df.iterrows():
                if r.variant == variants.CHAMPION:
                    continue
                d = r.to_dict()
                d["fold_exp_R"] = json.loads(d["fold_exp_R"])
                ok, why = leaderboard.promotable(d, c)
                print(f"  {r.variant:<24} {'PROMOTABLE (research)' if ok else 'no: ' + '; '.join(why)}")
    else:
        for r in results:
            print(f"HOLDOUT {r['variant']}: trades={r['trades']} expR={r['exp_R']:+.3f} dd={r['max_dd']*100:.1f}%")
    print(f"runtime {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
