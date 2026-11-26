"""Options ML (learning mode): a plain-numpy logistic regression for P(option trade return > 0).

It only ranks candidates, picks exploration arms and records outcomes. It never changes the budget,
the caps or the gates. Inputs available both in the training data and live at decision time:
the underlying's daily features (the live signal's ``features`` dict keys equal the dataset's
``f_*`` columns without the prefix), the contract delta, DTE and IV.

Training data: modelled call trades (source=options_entry in data/ml_dataset/trades.csv.gz) plus the
live outcomes in the ``option_outcomes`` table (weighted OPTIONS_ML_LIVE_WEIGHT).
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from .config import MODEL_DIR

log = logging.getLogger("app.options_ml")

MODEL_VERSION = "olr-1"
MODEL_PATH = MODEL_DIR / "options_ml.json"
# Relative (scale-free) daily features only: price levels (close, sma*, high*, atr14 ...) are left out.
FEATURES = [
    "rsi14", "adx14", "atr_pct", "atr_pct_rank", "rv20", "pct_vs_sma20", "pct_vs_sma50",
    "sma20_slope_pct", "pct_from_high10", "chg_5d_pct", "chg_20d_pct", "days_below_sma20",
    "vol_vs_20d_avg", "pct_vs_sma200", "ret_63d", "ret_126d", "ret_252d", "ret_12_1", "vol60",
    "vol60_rank", "rs_63d", "rs_126d", "pct_from_high252",
]
EXTRA = ["delta", "dte", "iv"]
ALL_FEATURES = FEATURES + EXTRA
VAL_START = "2025-01-01"
MIN_LIVE_ONLY_ROWS = 20
DELTA_ARMS = (0.55, 0.70, 0.80)
DTE_ARMS = (21, 30, 45)
_VARIANT = re.compile(r"call_d(\d+)_dte(\d+)")


# -- numerics ---------------------------------------------------------------------------------
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logreg(X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None, l2: float = 5.0,
               iters: int = 40) -> np.ndarray:
    """Weighted L2 logistic regression by Newton steps. Returns [bias, w1..wd]."""
    n, d = X.shape
    A = np.hstack([np.ones((n, 1)), X])
    w = np.ones(n) if w is None else np.asarray(w, float)
    beta = np.zeros(d + 1)
    reg = np.eye(d + 1) * l2
    reg[0, 0] = 0.0
    for _ in range(iters):
        p = _sigmoid(A @ beta)
        grad = A.T @ (w * (p - y)) + reg @ beta
        H = (A * (w * p * (1 - p))[:, None]).T @ A + reg + np.eye(d + 1) * 1e-8
        step = np.linalg.solve(H, grad)
        beta -= step
        if float(np.max(np.abs(step))) < 1e-7:
            break
    return beta


def auc_score(y: np.ndarray, score: np.ndarray) -> float | None:
    """Rank-based AUC (ties averaged). None when one class is missing."""
    y = np.asarray(y, float)
    pos = int(y.sum())
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        return None
    order = np.argsort(score, kind="mergesort")
    ranks = np.empty(len(score))
    s = np.asarray(score)[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def _fit_scaled(X: np.ndarray, y: np.ndarray, w: np.ndarray | None):
    """Median-impute, standardize, fit. Returns (beta, medians, means, stds)."""
    med = np.array([np.nanmedian(X[:, j]) if np.isfinite(X[:, j]).any() else 0.0
                    for j in range(X.shape[1])])
    Xi = np.where(np.isfinite(X), X, med)
    mean = Xi.mean(axis=0)
    std = Xi.std(axis=0)
    std = np.where(std < 1e-9, 1.0, std)
    Z = np.clip((Xi - mean) / std, -5, 5)
    return fit_logreg(Z, y, w), med, mean, std


def _transform(X, med, mean, std):
    Xi = np.where(np.isfinite(X), X, med)
    return np.clip((Xi - mean) / std, -5, 5)


# -- model file -------------------------------------------------------------------------------
def save_model(model: dict, path: Path | None = None) -> Path:
    path = Path(path or MODEL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(model, indent=1), encoding="utf-8")
    tmp.replace(path)
    _cache.clear()
    return path


_cache: dict = {}


def load_model(path: Path | None = None) -> dict | None:
    path = Path(path or MODEL_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
        if not m.get("weights") or m.get("features") != ALL_FEATURES:
            m = None
    except (OSError, ValueError):
        m = None
    _cache[str(path)] = (mtime, m)
    return m


def _vector(feats: dict | None, delta, dte, iv) -> np.ndarray:
    f = feats or {}
    row = []
    for k in FEATURES:
        v = f.get(k)
        row.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v) else np.nan)
    for v in (delta, dte, iv):
        try:
            v = abs(float(v)) if v is not None else np.nan
        except (TypeError, ValueError):
            v = np.nan
        row.append(v if math.isfinite(v) else np.nan)
    return np.array(row, float)


def predict(sig_features: dict | None, delta, dte, iv, *, path: Path | None = None) -> float | None:
    """P(option return > 0), or None when there is no model."""
    m = load_model(path)
    if m is None:
        return None
    x = _vector(sig_features, delta, dte, iv)[None, :]
    z = _transform(x, np.array(m["medians"]), np.array(m["mean"]), np.array(m["std"]))
    b = np.array(m["weights"])
    return float(_sigmoid(b[0] + z @ b[1:])[0])


def model_version(path: Path | None = None) -> str | None:
    m = load_model(path)
    return f"{m['version']}@{str(m.get('trained_at', ''))[:10]}" if m else None


# -- training data ----------------------------------------------------------------------------
def backtest_frame(dataset: Path):
    """Rows of the modelled options trades: (X, y, dates)."""
    import pandas as pd
    head = pd.read_csv(dataset, nrows=0).columns
    cols = ["source", "variant", "signal_date", "ret", "opt_iv"] + [f"f_{k}" for k in FEATURES
                                                                     if f"f_{k}" in head]
    df = pd.read_csv(dataset, usecols=cols, low_memory=False)
    df = df[(df["source"] == "options_entry") & df["ret"].notna()].copy()
    mm = df["variant"].astype(str).str.extract(_VARIANT)
    df["delta"] = pd.to_numeric(mm[0], errors="coerce") / 100.0
    df["dte"] = pd.to_numeric(mm[1], errors="coerce")
    df = df[df["delta"].notna() & df["dte"].notna()]
    X = np.column_stack([pd.to_numeric(df.get(f"f_{k}"), errors="coerce").to_numpy(float)
                         if f"f_{k}" in df else np.full(len(df), np.nan) for k in FEATURES]
                        + [df["delta"].to_numpy(float), df["dte"].to_numpy(float),
                           pd.to_numeric(df["opt_iv"], errors="coerce").to_numpy(float)])
    y = (df["ret"].to_numpy(float) > 0).astype(float)
    return X, y, df["signal_date"].astype(str).to_numpy()


def live_training_rows(db) -> list[dict]:
    """Closed live option trades usable for training: features snapshot, delta, dte, iv, return."""
    out = []
    for r in db.option_outcomes(5000):
        sel = r.get("selection") or {}
        if r.get("ret") is None:
            continue
        out.append({"proposal_id": r["proposal_id"], "features": sel.get("features") or {},
                    "delta": sel.get("delta"), "dte": sel.get("dte"), "iv": sel.get("iv"),
                    "ret": float(r["ret"]), "closed_at": r.get("closed_at"),
                    "opened_at": r.get("opened_at")})
    return out


def drop_holdout_rows(live_rows: list[dict]) -> list[dict]:
    """M7: live outcomes opened or closed on/after HOLDOUT_START stay out of training and validation."""
    from .ml_data.schema import HOLDOUT_START
    kept = [r for r in live_rows
            if not any(str(r.get(k) or "")[:10] >= HOLDOUT_START for k in ("opened_at", "closed_at"))]
    if len(kept) != len(live_rows):
        log.info("options ML: excluded %d live outcome(s) dated on/after the locked holdout %s",
                 len(live_rows) - len(kept), HOLDOUT_START)
    return kept


def _live_matrix(rows: list[dict]):
    if not rows:
        return np.empty((0, len(ALL_FEATURES))), np.empty(0)
    X = np.vstack([_vector(r["features"], r["delta"], r["dte"], r["iv"]) for r in rows])
    return X, np.array([1.0 if r["ret"] > 0 else 0.0 for r in rows])


def train(live_rows: list[dict] | None = None, *, dataset: Path | None = None,
          live_weight: float = 5.0, path: Path | None = None, save: bool = True) -> dict | None:
    """Fit and (optionally) save the model. Returns the report dict, or None when there is too
    little data (no dataset and fewer than MIN_LIVE_ONLY_ROWS live outcomes)."""
    from .ml_data import DATASET_PATH
    dataset = Path(dataset or DATASET_PATH)
    live_rows = drop_holdout_rows(live_rows or [])
    Xl, yl = _live_matrix(live_rows)
    have_ds = dataset.exists()
    if not have_ds and len(live_rows) < MIN_LIVE_ONLY_ROWS:
        return None
    Xb, yb, dates = (backtest_frame(dataset) if have_ds
                     else (np.empty((0, len(ALL_FEATURES))), np.empty(0), np.empty(0, str)))
    if len(Xb) + len(Xl) < MIN_LIVE_ONLY_ROWS:
        return None
    auc = None
    n_val = 0
    if len(Xb):
        tr = dates < VAL_START
        va = ~tr
        if tr.sum() >= 50 and va.sum() >= 20:
            # M10: live rows dated on/after the cut belong to validation, never to training.
            ltr = np.array([str(r.get("closed_at") or "")[:10] < VAL_START for r in live_rows], bool)
            Xlt, ylt = Xl[ltr], yl[ltr]
            Xt = np.vstack([Xb[tr], Xlt]) if len(Xlt) else Xb[tr]
            yt = np.concatenate([yb[tr], ylt]) if len(Xlt) else yb[tr]
            wt = np.concatenate([np.ones(int(tr.sum())), np.full(len(Xlt), live_weight)])
            beta, med, mean, std = _fit_scaled(Xt, yt, wt)
            Xv = np.vstack([Xb[va], Xl[~ltr]]) if (~ltr).any() else Xb[va]
            yv = np.concatenate([yb[va], yl[~ltr]]) if (~ltr).any() else yb[va]
            sc = _sigmoid(beta[0] + _transform(Xv, med, mean, std) @ beta[1:])
            auc = auc_score(yv, sc)
            n_val = int(len(yv))
    Xa = np.vstack([Xb, Xl]) if len(Xl) else Xb
    ya = np.concatenate([yb, yl]) if len(Xl) else yb
    wa = np.concatenate([np.ones(len(Xb)), np.full(len(Xl), live_weight)])
    beta, med, mean, std = _fit_scaled(Xa, ya, wa)
    baseline = float(np.average(ya, weights=wa))
    model = {
        "version": MODEL_VERSION, "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "features": ALL_FEATURES, "weights": [float(v) for v in beta],
        "medians": [float(v) for v in med], "mean": [float(v) for v in mean],
        "std": [float(v) for v in std], "n_backtest": int(len(Xb)), "n_live": int(len(Xl)),
        "val_auc": None if auc is None else round(auc, 4), "n_val": n_val,
        "baseline_win_rate": round(baseline, 4), "live_weight": live_weight,
    }
    if save:
        save_model(model, path)
    return model


def report_line(m: dict) -> str:
    auc = m.get("val_auc")
    return (f"Options ML trained: {m['n_backtest']} modelled + {m['n_live']} live rows, "
            f"validation AUC {'n/a' if auc is None else f'{auc:.3f}'} (0.50 = no skill), "
            f"baseline win rate {m['baseline_win_rate']:.1%}")


def retrain_due(settings, path: Path | None = None, dataset: Path | None = None,
                n_live: int = 0) -> bool:
    """True when the model is missing (and there is data to train on) or older than the interval."""
    from .ml_data import DATASET_PATH
    have_ds = Path(dataset or DATASET_PATH).exists()
    m = load_model(path)
    if m is None:
        return have_ds or n_live >= MIN_LIVE_ONLY_ROWS
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(m["trained_at"])).days
    except (KeyError, ValueError):
        return True
    return age >= int(settings.options_ml_retrain_days) and (have_ds or n_live >= MIN_LIVE_ONLY_ROWS)


# -- exploration ------------------------------------------------------------------------------
def explore_arm(run_id, symbol: str, day: date | str, s) -> tuple[float, int] | None:
    """Deterministic draw from run_id+symbol+date: with probability OPTIONS_ML_EXPLORE_PCT return a
    random (delta, dte) arm clamped to the configured option bounds, else None."""
    rng = random.Random(f"{run_id}|{symbol}|{day}")
    if rng.random() >= float(s.options_ml_explore_pct):
        return None
    delta = rng.choice(DELTA_ARMS)
    dte = rng.choice(DTE_ARMS)
    delta = min(max(delta, float(s.options_delta_min)), float(s.options_delta_max))
    dte = min(max(dte, int(s.options_min_dte)), int(s.options_max_dte))
    return round(delta, 2), int(dte)


def signal_features(sig: dict) -> dict:
    """Numeric model features of a live signal (what is stored in the selection at entry)."""
    f = sig.get("features") or {}
    return {k: f[k] for k in FEATURES
            if isinstance(f.get(k), (int, float)) and not isinstance(f.get(k), bool)
            and math.isfinite(f[k])}


# -- outcomes ---------------------------------------------------------------------------------
def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def expiry_intrinsic(symbol: str, exp: str, close_lookup=None) -> float | None:
    """Intrinsic value per share at expiry from the underlying's close on the expiry date.
    None when the close is unavailable (caller skips and retries later)."""
    from .engine.util import parse_occ
    occ = parse_occ(symbol)
    if not occ:
        return None
    try:
        if close_lookup is None:
            def close_lookup(u, d):
                from .sleeves import data
                df = data.daily_closes([u], "3mo")
                if df.empty or u not in df.columns:
                    return None
                s = df[u].dropna()
                s = s[[str(i.date()) == d for i in s.index]]
                return float(s.iloc[-1]) if len(s) else None
        c = close_lookup(occ["underlying"], str(exp)[:10])
    except Exception:  # noqa: BLE001 - no price source: retry later
        return None
    if c is None or not math.isfinite(c) or c <= 0:
        return None
    k = occ["strike"]
    return max(0.0, (c - k) if occ["option_type"] == "CALL" else (k - c))


def record_closed_outcomes(db, held_symbols: set[str], today: date, *,
                           positions_ok: bool = True, close_lookup=None) -> int:
    """Write one option_outcomes row for every auto option BUY whose position is gone.

    Exit premium: the ledger's exit fill when it has one, else the limit of the latest placed
    SELL_TO_CLOSE for that contract, else 0 for a contract that expired while held. Returns the
    number of rows written."""
    rows = db.proposals(400)
    buys = [r for r in rows if r.get("side") == "BUY" and _json(r.get("instrument_json")).get("selection")
            and str(r.get("position_intent") or "").upper() == "BUY_TO_OPEN"
            and r.get("status") in ("placed", "unknown", "filled")]
    if not positions_ok:
        return 0                     # positions fetch failed: "not held" would be a false signal
    n = 0
    for b in sorted(buys, key=lambda r: r["id"]):
        sym = b["symbol"]
        if sym in held_symbols or db.has_option_outcome(b["id"]):
            continue
        later_buys = [r["id"] for r in rows if r["symbol"] == sym and r.get("side") == "BUY"
                      and r["id"] > b["id"]]
        cap = min(later_buys) if later_buys else 10 ** 12
        sells = [r for r in rows if r["symbol"] == sym and r.get("side") == "SELL"
                 and b["id"] < r["id"] < cap and r.get("status") in ("placed", "unknown", "filled")
                 and str(r.get("position_intent") or "").upper() == "SELL_TO_CLOSE"]
        trade = None
        try:
            trade = db.get_trade(b["id"])
        except Exception:  # noqa: BLE001
            trade = None
        entry = (trade or {}).get("entry_fill_price") or b.get("limit_price")
        if not entry or entry <= 0:
            continue
        if sells:
            last = max(sells, key=lambda r: r["id"])
            exit_px = (trade or {}).get("exit_fill_price") or last.get("avg_fill_price") \
                or last.get("fill_price") or last.get("limit_price")
            reason = str(last.get("reason") or "").replace("option exit: ", "") or "exit"
            closed_at = (trade or {}).get("exit_fill_time") or last.get("placed_at") or last["created_at"]
        else:
            exp = b.get("expiration_date") or _json(b.get("instrument_json")).get("expiration_date")
            filled = bool((trade or {}).get("entry_fill_price"))
            if not (filled and exp and str(exp) < today.isoformat()):
                continue
            exit_px = expiry_intrinsic(sym, str(exp), close_lookup)
            if exit_px is None:
                continue             # no underlying close yet: retry on a later pass
            reason, closed_at = "expired", f"{exp}T20:00:00+00:00"
        if exit_px is None:
            continue
        sel = _json(b.get("instrument_json")).get("selection") or {}
        db.add_option_outcome({
            "proposal_id": b["id"], "symbol": sym,
            "underlying": _json(b.get("instrument_json")).get("underlying"),
            "entry_premium": float(entry), "exit_premium": float(exit_px),
            "ret": float(exit_px) / float(entry) - 1.0, "exit_reason": reason,
            "opened_at": (trade or {}).get("entry_fill_time") or b.get("placed_at") or b["created_at"],
            "closed_at": closed_at, "selection_json": json.dumps(sel),
            "ml_p": sel.get("ml_p") if sel.get("ml_version") else None,
            "explore": 1 if sel.get("explore") else 0,
        })
        n += 1
    return n
