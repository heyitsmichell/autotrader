"""TA v3 vs v2 evaluation (research folds only; bars after the holdout cutoff are never loaded).

Part A: live variant-d consensus entry gate, scored by v2 vs v3, exits d and d_time40, with the v3
        CONSENSUS_TA_MIN calibrated so trade counts match v2 (+-20%) and also at the live threshold.
Part B: v3 as a cross-sectional ranker (top-10 weekly) vs plain 12-1 momentum top-10 weekly.
Nothing here edits app/backtest or the harness: signal_at / segment_metrics are wrapped for the run.
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from .. import ta
from ..indicators import feature_series
from . import harness
from .variants import Variant

OUT = harness.EXP_DIR / "ta_v3"
REGISTRY = harness.EXP_DIR / "trials_registry.csv"
LIVE_TA_MIN = 0.15
COST_BPS = 6.0   # one-way per unit traded for the ranker (5 bps slippage + ~1 bp fees)


def registry_count() -> int:
    try:
        with open(REGISTRY, newline="") as f:
            return max(sum(1 for _ in f) - 1, 1)
    except OSError:
        return 1


def _log_trial(strategy, chash, universe, start, end, cagr, sharpe, maxdd, turnover, notes):
    new = not REGISTRY.exists()
    with open(REGISTRY, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "lab", "strategy", "config_hash", "universe", "start", "end", "cagr",
                        "sharpe", "maxdd", "turnover", "notes"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), "ta_v3", strategy, chash, universe,
                    start, end, round(cagr, 4), round(sharpe, 3), round(-abs(maxdd), 4),
                    round(turnover, 2), notes])


def deflated_sharpe(daily_ret, n_trials: int) -> float:
    """Bailey/Lopez de Prado deflated Sharpe: probability the true Sharpe exceeds the best-of-N null."""
    r = np.asarray(daily_ret, float)
    r = r[np.isfinite(r)]
    t = len(r)
    if t < 30 or r.std(ddof=1) == 0 or n_trials < 2:
        return float("nan")
    sr = r.mean() / r.std(ddof=1)
    z = r - r.mean()
    skew = float((z ** 3).mean() / (z.std() ** 3)) if z.std() > 0 else 0.0
    kurt = float((z ** 4).mean() / (z.std() ** 4)) if z.std() > 0 else 3.0
    nd = NormalDist()
    g = 0.5772156649015329
    sr0 = math.sqrt(1.0 / (t - 1)) * ((1 - g) * nd.inv_cdf(1 - 1 / n_trials)
                                      + g * nd.inv_cdf(1 - 1 / (n_trials * math.e)))
    den = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr * sr, 1e-9))
    return nd.cdf((sr - sr0) * math.sqrt(t - 1) / den)


# ---- Part A: entry-gate backtest -------------------------------------------------------------
def _gate_worker(job: dict) -> dict:
    """job: {scoring: v2|v3, ta_min, exit_style}. Runs in a worker process (env/patches are local)."""
    from ..backtest import engine
    os.environ["TA_MODEL"] = job["scoring"]
    os.chdir(harness.ROOT)
    src = harness.ROOT / "data" / "bars_yahoo"
    cutoff, _ = harness.data_cutoff(src)
    bars_dir = harness.research_dir(src, cutoff)
    spy = pd.read_csv(bars_dir / "SPY.csv", parse_dates=["date"]).set_index("date").close
    spyc = {d.date(): float(v) for d, v in spy.items()}

    orig_hook, orig_seg = harness.entry_hook, harness.segment_metrics

    def add_rs(blist, features, index):
        f = features[index] if 0 <= index < len(features) else None
        if f is not None and index >= 126 and "rs_126d_done" not in f:
            d1, d0 = blist[index]["time"].date(), blist[index - 126]["time"].date()
            b1, b0 = spyc.get(d1), spyc.get(d0)
            if b1 and b0 and blist[index - 126]["close"]:
                f["rs_126d"] = (blist[index]["close"] / blist[index - 126]["close"] - b1 / b0) * 100
            f["rs_126d_done"] = True

    @contextlib.contextmanager
    def hook_rs(*a, **kw):
        # run_backtest(signal_fn=...) hook: inject rs_126d, then delegate (engine.signal_at untouched)
        with orig_hook(*a, **kw) as hooks:
            inner = hooks["signal_fn"]

            def sig(symbol, blist, features, index, settings, **skw):
                add_rs(blist, features, index)
                return inner(symbol, blist, features, index, settings, **skw)
            yield {**hooks, "signal_fn": sig}

    def seg(res, s0, s1):
        m = orig_seg(res, s0, s1)
        m["perf"] = harness.equity_perf(res, s0, s1)
        return m

    if job["scoring"] == "v3":
        harness.entry_hook = hook_rs
    harness.segment_metrics = seg
    try:
        name = f"ta_{job['scoring']}|{job['exit_style']}|min{job['ta_min']:.2f}"
        v = Variant(name, "consensus", job["exit_style"], False, {"consensus_ta_min": job["ta_min"]})
        r = harness.run_variant(v, "research", n_trials=registry_count())
    finally:
        harness.entry_hook, harness.segment_metrics = orig_hook, orig_seg
    r["job"] = job
    r.pop("trade_rs", None)
    return r


def run_gate(jobs: list[dict], workers: int = 4) -> list[dict]:
    from multiprocessing import Pool
    with Pool(workers) as p:
        return p.map(_gate_worker, jobs)


def calibrate(workers: int = 4, grid=(0.15, 0.25, 0.35, 0.45, 0.55, 0.65)) -> dict:
    """v2 at live threshold; v3 across a threshold grid (exit d); pick v3 min with trades closest to v2's
    and within +-20%. Then run exit time40 for v2@live, v3@live and v3@matched."""
    res = run_gate([{"scoring": "v2", "ta_min": LIVE_TA_MIN, "exit_style": "d"}] +
                   [{"scoring": "v3", "ta_min": g, "exit_style": "d"} for g in grid], workers)
    v2 = res[0]
    n2 = v2["trades"]
    ranked = sorted(res[1:], key=lambda r: abs(r["trades"] - n2))
    best = ranked[0]
    matched = best["job"]["ta_min"] if abs(best["trades"] - n2) <= 0.2 * n2 else None
    extra_jobs = [{"scoring": "v2", "ta_min": LIVE_TA_MIN, "exit_style": "d_time40"},
                  {"scoring": "v3", "ta_min": LIVE_TA_MIN, "exit_style": "d_time40"}]
    if matched is not None:
        extra_jobs.append({"scoring": "v3", "ta_min": matched, "exit_style": "d_time40"})
    return {"v2_live_d": v2, "v3_grid_d": res[1:], "matched_min": matched, "closest": best["job"]["ta_min"],
            "time40": run_gate(extra_jobs, workers)}


# ---- Part B: cross-sectional ranker -----------------------------------------------------------
def _load_bar_lists(bars_dir: Path) -> dict:
    out = {}
    for f in sorted(Path(bars_dir).glob("*.csv")):
        df = pd.read_csv(f, parse_dates=["date"]).dropna()
        out[f.stem] = [{"time": pd.Timestamp(r.date).to_pydatetime().replace(tzinfo=__import__("datetime").timezone.utc),
                        "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
                       for r in df.itertuples()]
    return out


def ranker_returns(bars_dir: Path, top_n: int = 10) -> dict:
    """Daily return series for v3-score top-N and 12-1 top-N, weekly rebalance (last trading day of the
    ISO week), equal weight. Weights decided at close t earn returns from close t+1 onward (one extra
    day of lag, conservative); COST_BPS per unit of one-way turnover."""
    bl = _load_bar_lists(bars_dir)
    spy = bl["SPY"]
    syms = [s for s in bl if s != "SPY"]
    close = pd.DataFrame({s: pd.Series({b["time"].date(): b["close"] for b in bl[s]}) for s in syms}).sort_index()
    score = {"v3": {}, "mom121": {}}
    for s in syms:
        rows = feature_series(bl[s], bench=spy)
        for f in rows:
            d = pd.Timestamp(f["date"]).date()
            if f.get("ret_12_1") is None:
                continue
            score["mom121"].setdefault(d, {})[s] = f["ret_12_1"]
            score["v3"].setdefault(d, {})[s] = ta._score_v3(f)[0]
    dates = list(close.index)
    ts = pd.DatetimeIndex(pd.to_datetime(dates))
    wk = pd.Series(range(len(dates)), index=ts).groupby([ts.isocalendar().year, ts.isocalendar().week]).max()
    reb = {dates[i] for i in wk.values}
    ret = close.pct_change().fillna(0.0)
    out = {}
    for name in score:
        w = pd.Series(0.0, index=syms)
        cur_target, daily, turn_total = None, [], 0.0
        pending = []   # (apply_from_index, weights)
        for i, d in enumerate(dates):
            for item in [p for p in pending if p[0] == i]:
                new = item[1]
                t = float((new - w).abs().sum())
                cost = t * COST_BPS / 1e4
                turn_total += t
                w = new
                cur_cost = cost
                pending.remove(item)
                break
            else:
                cur_cost = 0.0
            daily.append(float((w * ret.loc[d].fillna(0.0)).sum()) - cur_cost)
            if d in reb and d in score[name]:
                sc = pd.Series(score[name][d]).dropna().sort_values(ascending=False, kind="stable")
                pick = list(sc.index[:top_n])
                nw = pd.Series(0.0, index=syms)
                if len(pick) == top_n:
                    nw[pick] = 1.0 / top_n
                    pending.append((i + 2, nw))   # decided at close i, trade close i+1, earn from i+2
        out[name] = (pd.Series(daily, index=dates), turn_total)
    return out


def ranker_report() -> dict:
    src = harness.ROOT / "data" / "bars_yahoo"
    cutoff, all_dates = harness.data_cutoff(src)
    bars_dir = harness.research_dir(src, cutoff)
    rd, _, _ = harness.split_dates(all_dates)
    folds = harness.make_folds(rd)
    rr = ranker_returns(bars_dir)
    n_tr = registry_count()
    rep = {}
    for name, (r, turn) in rr.items():
        full = r[(r.index >= folds[0][1]) & (r.index <= rd[-1])]
        eq = (1 + full).cumprod()
        perf = harness.perf_from_equity(list(eq.index), eq.values)
        per_fold = []
        for _te, s0, s1 in folds:
            seg = r[(r.index >= s0) & (r.index <= s1)]
            e = (1 + seg).cumprod()
            per_fold.append(harness.perf_from_equity(list(e.index), e.values))
        yrs = (full.index[-1] - full.index[0]).days / 365.25
        rep[name] = {"perf": perf, "folds": per_fold, "turnover_per_yr": turn / yrs,
                     "dsr": deflated_sharpe(full.values, n_tr), "n_trials": n_tr}
    spy = pd.read_csv(bars_dir / "SPY.csv", parse_dates=["date"]).set_index("date").close
    spy.index = [d.date() for d in spy.index]
    sp = spy[(spy.index >= folds[0][1]) & (spy.index <= rd[-1])]
    rep["SPY_buyhold"] = {"perf": harness.perf_from_equity(list(sp.index), sp.values)}
    rep["window"] = [str(folds[0][1]), str(rd[-1])]
    return rep


# ---- driver -----------------------------------------------------------------------------------
def run_all(workers: int = 4) -> dict:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    cal = calibrate(workers)
    rk = ranker_report()
    n_tr = registry_count()
    out = {"calibration": cal, "ranker": rk, "registry_count_before": n_tr, "seconds": time.time() - t0}
    (OUT / "ta_v3_results.json").write_text(json.dumps(out, indent=1, default=str))

    a, b = rk["window"]
    allgate = [cal["v2_live_d"], *cal["v3_grid_d"], *cal["time40"]]
    for r in allgate:
        p, j = r["perf"], r["job"]
        yrs = max((pd.Timestamp(b) - pd.Timestamp(a)).days / 365.25, 1e-9)
        _log_trial(r["variant"], r["config_hash"], "bars_yahoo_tech_semis", a, b, p["cagr"], p["sharpe"],
                   p["max_dd"], r["trades"] / yrs, f"gate {j['scoring']} min={j['ta_min']} exit={j['exit_style']} "
                   f"expR={r['exp_R']:+.3f} trades={r['trades']}; research folds only")
    for name in ("v3", "mom121"):
        p = rk[name]["perf"]
        _log_trial(f"rank_top10_weekly|{name}", hashlib.sha1(name.encode()).hexdigest()[:10],
                   "bars_yahoo_87", a, b, p["cagr"], p["sharpe"], p["max_dd"], rk[name]["turnover_per_yr"],
                   "ranker top10 weekly eq-wt; 6bps/side; research folds only")
    return out
