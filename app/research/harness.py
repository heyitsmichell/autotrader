"""Experiment harness: research set vs LOCKED 365-day holdout, walk-forward folds, penalized score.

Rule variants have nothing fitted, so a research run backtests on bars truncated before the holdout
cutoff (holdout rows are never loaded) and slices trades by entry date into walk-forward folds.
ML-filter variants retrain per fold on rows whose labels resolved before the test fold (plus embargo).
"""
from __future__ import annotations

import contextlib
import csv
import dataclasses
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .. import ml_candles
from .variants import CUSTOM_ENTRIES, REGISTRY, Variant

ROOT = Path(__file__).resolve().parent.parent.parent
EXP_DIR = ROOT / "data" / "experiments"
HOLDOUT_LOG = EXP_DIR / "holdout_log.csv"
HOLDOUT_DAYS = 365
EMBARGO = 11
N_FOLDS = 5
ML_TOP_Q = 0.8
FEES = {"sec_fee_rate": 0.0000278, "taf_per_share": 0.000166}
OVERRIDES_BASE = {"backtest_commission_per_order": 0.0, "backtest_slippage_bps": 5.0}


# ---- splits ---------------------------------------------------------------------------------
def _d(x) -> date:
    return pd.Timestamp(x).date()


def split_dates(dates, holdout_days: int = HOLDOUT_DAYS):
    """-> (research_dates, holdout_dates, cutoff). Holdout = last `holdout_days` calendar days."""
    u = sorted({_d(x) for x in dates})
    cutoff = u[-1] - timedelta(days=holdout_days)
    return [x for x in u if x < cutoff], [x for x in u if x >= cutoff], cutoff


def make_folds(research_dates, n_folds: int = N_FOLDS, embargo: int = EMBARGO):
    """Expanding walk-forward folds on research dates only: (train_end, test_start, test_end)."""
    f = ml_candles.walk_forward_folds(np.asarray(sorted(research_dates), dtype="datetime64[ns]"),
                                      n_folds, embargo)
    return [(_d(te), _d(s0), _d(s1)) for (_t0, te, s0, s1) in f]


def holdout_segment(all_dates, cutoff, embargo: int = EMBARGO):
    u = sorted({_d(x) for x in all_dates})
    i = next(i for i, d in enumerate(u) if d >= cutoff)
    return (u[i - embargo - 1], u[i], u[-1])


# ---- data -----------------------------------------------------------------------------------
def research_dir(src: Path, cutoff: date) -> Path:
    """Copy of the bars with every row on/after the cutoff removed (holdout never reaches the engine)."""
    out = EXP_DIR / "_cache" / f"research_bars_{cutoff.isoformat()}"
    marker = out / ".done"
    if marker.exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    for f in Path(src).glob("*.csv"):
        df = pd.read_csv(f)
        df = df[pd.to_datetime(df["date"]).dt.date < cutoff]
        df.to_csv(out / f.name, index=False)
    marker.write_text("ok")
    return out


def data_cutoff(src: Path) -> tuple[date, list[date]]:
    spy = pd.read_csv(Path(src) / "SPY.csv", usecols=["date"])
    dates = [_d(x) for x in spy["date"]]
    _, _, cutoff = split_dates(dates)
    return cutoff, dates


# ---- ML filter ------------------------------------------------------------------------------
def ml_pass_map(D: pd.DataFrame, feat: list[str], segments, fit_fn=None, top_q: float = ML_TOP_Q, seed=0):
    """segments: [(train_end, test_start, test_end)]. Trains ONLY on rows with date <= train_end AND
    exit_date < test_start. Threshold = top_q quantile of the model's TRAIN scores (causal).
    Returns (set of (date, symbol) passing, audit rows (max train date, max train exit_date, test_start))."""
    fit_fn = fit_fn or ml_candles.fit_predict
    passed, audit = set(), []
    dts = pd.Series([_d(x) for x in D["date"]], index=D.index)
    exd = pd.Series([_d(x) for x in D["exit_date"]], index=D.index)
    for tr_end, s0, s1 in segments:
        trm = (dts <= tr_end) & (exd < s0)
        tem = (dts >= s0) & (dts <= s1)
        tr, te = D[trm], D[tem]
        if len(tr) < 2000 or len(te) == 0:
            continue
        audit.append((dts[trm].max(), exd[trm].max(), s0))
        Xtr, Xte = tr[feat].values, te[feat].values
        sc = np.asarray(fit_fn("mlp", Xtr, tr.win.values, np.vstack([Xtr, Xte]), seed))
        thr = np.quantile(sc[:len(tr)], top_q)
        ok = sc[len(tr):] >= thr
        for sym, d in zip(te.sym.values[ok], dts[tem].values[ok]):
            passed.add((d, sym))
    return passed, audit


def _bar_key(d, o, h, c):
    return (d, round(float(o), 3), round(float(h), 3), round(float(c), 3))


# ---- entry hook -----------------------------------------------------------------------------
@contextlib.contextmanager
def entry_hook(variant: Variant, bars_dir: Path, ml_keys: set | None):
    """Replace engine.signal_at with a wrapper implementing the entry family and the ML gate.
    (app/backtest is not edited; the module attribute is swapped for the duration of the run.)"""
    from ..backtest import engine, signals
    from .. import ta
    orig = engine.signal_at
    bars = ml_candles.load_bars(str(bars_dir))
    mom_thr, key_to_sym = {}, {}
    if variant.entry == "momentum":
        r20 = pd.DataFrame({s: df.close.pct_change(20) for s, df in bars.items() if s != "SPY"})
        q = r20.quantile(0.8, axis=1)
        mom_thr = {_d(k): v for k, v in q.items() if np.isfinite(v)}
    if ml_keys is not None:
        for s, df in bars.items():
            for dt, o, h, c in zip(df.index, df.open.values, df.high.values, df.close.values):
                key_to_sym[_bar_key(_d(dt), o, h, c)] = s

    def custom(blist, features, index, settings):
        f = features[index] if 0 <= index < len(features) else None
        if f is None or index < 200:
            return {"ok": False, "reason": "warmup"}
        ok_el, why = signals.eligibility_ok(blist[:index + 1], index, f, settings)
        if not ok_el:
            return {"ok": False, "eligible": False, "reason": why}
        c, sma50 = f["close"], f.get("sma50")
        fam, hit = variant.entry, False
        if sma50 is None or f.get("atr14") is None:
            return {"ok": False, "reason": "no features"}
        if fam == "breakout20_vol":
            hi = max(b["high"] for b in blist[index - 20:index])
            hit = c > hi and (f.get("vol_vs_20d_avg") or 0) >= 1.5 and c > sma50
        elif fam == "momentum":
            thr = mom_thr.get(blist[index]["time"].date())
            chg = f.get("chg_20d_pct")
            hit = thr is not None and chg is not None and chg / 100.0 >= thr and c > sma50
        elif fam == "meanrev":
            m200 = sum(b["close"] for b in blist[index - 199:index + 1]) / 200
            rsi = f.get("rsi14")
            hit = rsi is not None and rsi < 30 and c > m200
        if not hit:
            return {"ok": False, "reason": fam}
        a = ta.analyze(f, settings)
        sl, tp = ta.levels(c, f["atr14"], settings)
        sig = {"symbol": "", "close": c, "atr": a.get("atr"), "ta_score": a.get("ta_score"),
               "trend": a.get("trend"), "ml_p": None, "features": f}
        return {"ok": sl is not None, "eligible": True, "signal": sig, "sl": sl, "tp": tp, "gate": {}}

    def hooked(blist, features, index, settings, **kw):
        res = custom(blist, features, index, settings) if variant.entry in CUSTOM_ENTRIES \
            else orig(blist, features, index, settings, **kw)
        if res.get("ok") and ml_keys is not None:
            b = blist[index]
            sym = key_to_sym.get(_bar_key(b["time"].date(), b["open"], b["high"], b["close"]))
            if (b["time"].date(), sym) not in ml_keys:
                return {"ok": False, "reason": "ML filter"}
        return res

    engine.signal_at = hooked
    try:
        yield
    finally:
        engine.signal_at = orig


# ---- metrics --------------------------------------------------------------------------------
def segment_metrics(res, start: date, end: date) -> dict:
    tr = [t for t in res.trades if t.exit_day and start <= t.entry_day <= end and t.risk_usd > 0]
    rs = [t.r_multiple for t in tr]
    pairs = [(d, e) for d, e in zip(res.equity_dates, res.equity_curve) if start <= d <= end]
    ret = dd = 0.0
    if len(pairs) > 2 and pairs[0][1] > 0:
        eq = [e for _, e in pairs]
        ret = eq[-1] / eq[0] - 1
        peak = eq[0]
        for v in eq:
            peak = max(peak, v)
            dd = max(dd, (peak - v) / peak if peak > 0 else 0.0)
    return {"start": str(start), "end": str(end), "trades": len(tr),
            "win_rate": sum(1 for t in tr if t.pnl > 0) / len(tr) if tr else 0.0,
            "exp_R": float(np.mean(rs)) if rs else 0.0, "r_sum": float(sum(rs)),
            "r_sq": float(sum(r * r for r in rs)), "ret": ret, "max_dd": dd}


def penalized_score(n: int, r_sum: float, r_sq: float, n_trials: int, k: float = 1.0):
    """mean expectancy - k * SE * sqrt(log(n_trials)); SE from pooled trade-R std (trades are correlated,
    so this is optimistic). Returns (mean, se, penalized)."""
    if n < 2:
        return 0.0, 0.0, 0.0
    mean = r_sum / n
    var = max(r_sq / n - mean * mean, 0.0) * n / (n - 1)
    se = math.sqrt(var / n)
    return mean, se, mean - k * se * math.sqrt(math.log(max(n_trials, 2)))


def build_settings(overrides: dict, data_dir: Path):
    from ..config import load_settings
    s = load_settings()
    base = {"backtest_data_dir": Path(data_dir), "backtest_results_dir": EXP_DIR / "_scratch",
            "backtest_live_exits": True, "backtest_ml_enabled": False, "backtest_min_bars": 250,
            "backtest_initial_cash": 25000.0, "backtest_max_gross_exposure_pct": 100.0,
            **OVERRIDES_BASE}
    return dataclasses.replace(s, **{**base, **overrides})


_DS_CACHE: dict = {}


def _ml_keys(scope: str, bars_dir: Path, cutoff: date, all_dates):
    key = (scope, str(bars_dir))
    if key in _DS_CACHE:
        return _DS_CACHE[key]
    D, feat = ml_candles.build_dataset(ml_candles.load_bars(str(bars_dir)), tgt_mult=3.0)
    if scope == "research":
        rd, _, _ = split_dates(all_dates)
        segs = make_folds(rd)
    else:
        segs = [holdout_segment(all_dates, cutoff)]
    _DS_CACHE[key] = ml_pass_map(D, feat, segs)[0]
    return _DS_CACHE[key]


def run_variant(variant: Variant | str, scope: str = "research", data_dir: Path | None = None,
                n_trials: int = len(REGISTRY)) -> dict:
    """scope='research' uses truncated bars; scope='holdout' runs full data and reports the holdout slice."""
    from ..backtest.engine import run_backtest
    from ..universe import get_predefined_universe
    variant = REGISTRY[variant] if isinstance(variant, str) else variant
    src = Path(data_dir or ROOT / "data" / "bars_yahoo")
    os.chdir(ROOT)
    t0 = time.time()
    cutoff, all_dates = data_cutoff(src)
    bars_dir = research_dir(src, cutoff) if scope == "research" else src
    ml_keys = _ml_keys(scope, bars_dir, cutoff, all_dates) if variant.ml_filter else None
    s = build_settings(variant.overrides(), bars_dir)
    universe = list(dict.fromkeys(get_predefined_universe("tech_semis") + [x.upper() for x in s.watchlist]))
    with entry_hook(variant, bars_dir, ml_keys):
        res = run_backtest(s, universe=universe, benchmark_symbol="SPY", **FEES)
    out = {"variant": variant.name, "config_hash": variant.config_hash(), "scope": scope,
           "cutoff": str(cutoff), "n_trials": n_trials}
    if scope == "research":
        rd, _, _ = split_dates(all_dates)
        folds = [segment_metrics(res, s0, s1) for _te, s0, s1 in make_folds(rd)]
        n = sum(f["trades"] for f in folds)
        m, se, pen = penalized_score(n, sum(f["r_sum"] for f in folds), sum(f["r_sq"] for f in folds), n_trials)
        out.update(folds=folds, trades=n, exp_R=m, se=se, penalized=pen,
                   all_positive=all(f["exp_R"] > 0 and f["trades"] > 0 for f in folds),
                   max_dd=max(f["max_dd"] for f in folds))
    else:
        h = segment_metrics(res, cutoff, all_dates[-1])
        out.update(holdout=h, trades=h["trades"], exp_R=h["exp_R"], se=0.0, penalized=0.0, max_dd=h["max_dd"])
    out["seconds"] = round(time.time() - t0, 1)
    return out


def log_holdout(variant: str, config_hash: str, path: Path = HOLDOUT_LOG) -> int:
    """Append a holdout peek; returns how many times this variant was peeked at before."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = 0
    if path.exists():
        with open(path, newline="") as f:
            prior = sum(1 for r in csv.DictReader(f) if r["variant"] == variant)
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["variant", "timestamp", "config_hash"])
        w.writerow([variant, datetime.now().isoformat(timespec="seconds"), config_hash])
    return prior
