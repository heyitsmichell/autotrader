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
    from app.research import harness
    return harness.run_variant(a[0], a[1], n_trials=a[2])


def main() -> int:
    from app.research import harness, leaderboard, variants
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", default="")
    ap.add_argument("--holdout", default="", help="LOCKED holdout (last 365d); each use is logged")
    ap.add_argument("--workers", type=int, default=1)
    a = ap.parse_args()
    if a.list:
        for v in variants.REGISTRY.values():
            print(f"{v.name:<24} entry={v.entry:<15} exit={v.exit_style:<8} ml={v.ml_filter!s:<5} "
                  f"{v.config_hash()}  {v.description}")
        return 0
    t0 = time.time()
    scope, names = ("holdout", a.holdout) if a.holdout else ("research", a.run)
    if not names:
        ap.print_help()
        return 1
    vs = variants.get(names)
    if scope == "holdout":
        if variants.CHAMPION not in [v.name for v in vs]:
            vs = [variants.REGISTRY[variants.CHAMPION]] + vs   # promotion needs the champion's holdout
        for v in vs:
            prior = harness.log_holdout(v.name, v.config_hash())
            print(f"HOLDOUT PEEK logged: {v.name} (previous peeks: {prior})")
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
