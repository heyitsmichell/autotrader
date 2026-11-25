"""Read-only helpers behind the Backtests tab (docs/BACKTESTS_TAB.md). Never raises for missing files."""
from __future__ import annotations

import asyncio
import csv
import json
import math
import time
from pathlib import Path

import numpy as np

from .config import env
from .pipeline import PIPELINE
from .sleeves import analytics, registry

LIVE_IDS = [s.id for s in registry.SLEEVES]
_TRIAL_LAB = {"mom_skip1w": "momentum_variants", "overnight_mom": "overnight_momentum",
              "vaa_g4": "rotation_catalog", "swing_dynamic": "swing_universe", "putwrite": "putwrite_mgmt",
              "momentum": "momentum_variants", "vaa": "rotation_catalog"}
_SHADOW_ROWS = {  # shadow id -> (csv path under data/, row name)
    "mom_skip1w": ("experiments/momentum_variants/leaderboard.csv", "sp500|N10|mom_skip1w"),
    "overnight_mom": ("experiments/overnight_momentum/leaderboard.csv", "sp500|N10|overnight_12m"),
    "vaa_g4": ("experiments/rotation/catalog_leaderboard.csv", "vaa_g4"),
}
_BIASED = {"mom_skip1w", "overnight_mom", "momentum"}
SWING_JSON = "backtests/swing_universe_2026-11-25/results.json"
SWING_WINDOW = "last3y_2022-11-21_2025-11-21"
PORTFOLIO_TTL = 24 * 3600


def _data() -> Path:
    return Path(env.DATA_DIR)


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _csv(path: Path) -> list[dict]:
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def _title(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#"):
                    return line.lstrip("#").strip()
    except OSError:
        pass
    return path.stem


def _evidence(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        fp = _data() / p
        ok = fp.is_file()
        out.append({"path": p, "title": _title(fp) if ok and p.endswith(".md") else Path(p).stem, "exists": ok})
    return out


# ---- live stats ------------------------------------------------------------------------
def _series(db, kind: str) -> list[dict]:
    if db is None:
        return []
    try:
        return db.sleeve_equity_series(3650) if kind == "sleeve" else db.shadow_equity_series(3650)
    except Exception:  # noqa: BLE001 - a DB problem must not break the tab
        return []


def _live_stats(rows: list[dict], key: str) -> dict | None:
    pts = [(r["day"], r.get(key)) for r in rows if r.get(key) is not None]
    if len(pts) < 2:
        return None
    rets = analytics.returns_from_equity(pts)
    m = analytics.metrics(rets)
    dd = analytics.drawdown_series(rets)
    return {"days": len(rets), "return_pct": (pts[-1][1] / pts[0][1] - 1) * 100,
            "sharpe": m["sharpe"], "max_dd": min(d for _, d in dd) if dd else None}


def _live_for(db, pid: str, cache: dict) -> dict | None:
    if pid in LIVE_IDS:
        return _live_stats(cache.setdefault("s", _series(db, "sleeve")), pid)
    return _live_stats(cache.setdefault("h", _series(db, "shadow")), pid)


# ---- backtest numbers ------------------------------------------------------------------
def _backtest(pid: str) -> tuple[dict | None, str | None]:
    if pid in LIVE_IDS:
        try:
            rets = analytics.load_reference_returns(pid)
        except (OSError, ValueError, KeyError):
            return None, f"reference returns for {pid} missing"
        m = analytics.metrics(rets)
        win = (analytics.bands().get(pid) or {}).get("window") or (
            f"{rets[0][0]}..{rets[-1][0]}" if rets else None)
        return {"cagr": m["cagr"], "vol": m["vol"], "sharpe": m["sharpe"], "max_dd": m["max_dd"],
                "window": win, "source": f"app/sleeves/reference/{pid}.csv.gz",
                "biased": pid in _BIASED}, None
    if pid in _SHADOW_ROWS:
        rel, name = _SHADOW_ROWS[pid]
        row = next((r for r in _csv(_data() / rel) if r.get("name") == name), None)
        if row is None:
            return None, f"{rel} or row {name} missing"
        n = _f(row.get("n_days"))
        return {"cagr": _f(row.get("cagr")), "vol": _f(row.get("vol")), "sharpe": _f(row.get("sharpe")),
                "max_dd": _f(row.get("maxdd")),
                "window": f"{row.get('start') or 'full history'}..2025-11 ({int(n)} days)" if n else row.get("start"),
                "source": f"{rel}:{name}", "biased": pid in _BIASED}, None
    if pid == "swing_dynamic":
        try:
            d = json.loads((_data() / SWING_JSON).read_text(encoding="utf-8"))
            w = d["windows"][SWING_WINDOW]["C"]
        except (OSError, ValueError, KeyError, TypeError):
            return None, f"{SWING_JSON} missing"
        mdd = _f(w.get("max_dd"))
        return {"cagr": _f(w.get("cagr")), "vol": None, "sharpe": _f(w.get("sharpe")),
                "max_dd": -abs(mdd) if mdd is not None else None,
                "window": f"{w.get('from')}..{w.get('to')}", "source": f"{SWING_JSON}:C",
                "biased": False}, None
    return None, "no backtest source"


def _trial_counts() -> dict:
    out: dict[str, int] = {}
    for r in _csv(_data() / "experiments" / "trials_registry.csv"):
        out[r.get("lab", "")] = out.get(r.get("lab", ""), 0) + 1
    return out


def _trials(pid: str, counts: dict) -> dict | None:
    lab = _TRIAL_LAB.get(pid)
    return {"lab": lab, "n": counts.get(lab, 0)} if lab and lab in counts else None


def strategies(db=None) -> dict:
    counts, cache, cards = _trial_counts(), {}, []
    entries = [e for e in PIPELINE if e["stage"] == "live"] + [e for e in PIPELINE if e["stage"] == "shadow"]
    for e in entries:
        bt, note = _backtest(e["id"])
        cards.append({"id": e["id"], "name": e["name"], "stage": e["stage"], "kind": e["kind"],
                      "since": e["since"], "verdict": e["verdict"], "backtest": bt,
                      "live": _live_for(db, e["id"], cache), "trials": _trials(e["id"], counts),
                      "evidence": _evidence(e["evidence"]), "compare_to": e["compare_to"], "note": note})
    return {"cards": cards}


def pipeline_view(db=None) -> dict:
    cache, stages = {}, {"live": [], "shadow": [], "rejected": []}
    for e in PIPELINE:
        item = {**e, "evidence": _evidence(e["evidence"])}
        if e["stage"] in ("live", "shadow"):
            item["live_stats"] = _live_for(db, e["id"], cache)
        if e["stage"] == "shadow" and e["compare_to"]:
            item["compare_stats"] = _live_for(db, e["compare_to"], cache)
        stages[e["stage"]].append(item)
    return {"stages": stages}


# ---- portfolio -------------------------------------------------------------------------
_pcache: dict = {"t": 0.0, "v": None}


def _weights(scheme: str, hist: np.ndarray) -> np.ndarray:
    n = hist.shape[1]
    eq = np.full(n, 1.0 / n)
    if scheme == "equal" or hist.shape[0] < 20:
        return eq
    if scheme == "invvol":
        v = hist.std(axis=0, ddof=1)
        if not np.all(v > 0):
            return eq
        w = 1.0 / v
        return w / w.sum()
    cov = np.cov(hist.T)
    if not np.all(np.isfinite(cov)) or not np.all(np.diag(cov) > 0):
        return eq
    w = eq.copy()
    for _ in range(500):  # multiplicative fixed point on w_i * (cov w)_i
        rc = w * (cov @ w)
        w = w * np.sqrt(np.mean(rc) / np.maximum(rc, 1e-18))
        w = w / w.sum()
    return w


def _run(scheme: str, R: np.ndarray, months: list[str], lookback: int = 126) -> np.ndarray:
    t, n = R.shape
    val = np.full(n, 1.0 / n)
    out = np.empty(t)
    for i in range(t):
        if i > 0 and months[i] != months[i - 1]:
            val = val.sum() * _weights(scheme, R[max(0, i - lookback):i])
        val = val * (1 + R[i])
        out[i] = val.sum()
    return out


def _stats(daily: np.ndarray) -> dict:
    m = analytics.metrics([("d", float(x)) for x in daily])
    return {k: m[k] for k in ("cagr", "vol", "sharpe", "max_dd")}


def _empty(note: str) -> dict:
    return {"window": None, "schemes": [], "curve": [], "correlation": None, "live": None, "note": note}


def compute_portfolio() -> dict:
    series = {}
    for sid in LIVE_IDS:
        try:
            series[sid] = dict(analytics.load_reference_returns(sid))
        except (OSError, ValueError, KeyError):
            return _empty(f"reference returns for {sid} missing")
    days = sorted(set.intersection(*(set(s) for s in series.values())))
    if len(days) < 30:
        return _empty("reference series have no usable common window")
    R = np.array([[series[s][d] for s in LIVE_IDS] for d in days])
    months = [d[:7] for d in days]
    curves = {k: _run(k, R, months) for k in ("equal", "invvol", "erc")}
    names = {"equal": "Equal 25%", "invvol": "Inverse volatility", "erc": "Equal risk"}
    schemes = []
    for k, v in curves.items():
        prev = np.concatenate(([1.0], v[:-1]))
        schemes.append({"name": names[k], **_stats(v / prev - 1)})
    idx = list(range(0, len(days), 5))
    if idx[-1] != len(days) - 1:
        idx.append(len(days) - 1)
    curve = [{"day": days[i], "equal": float(curves["equal"][i] - 1), "invvol": float(curves["invvol"][i] - 1)}
             for i in idx]
    corr = np.corrcoef(R.T)
    return {"window": f"{days[0]}..{days[-1]}", "schemes": schemes, "curve": curve,
            "correlation": {"ids": LIVE_IDS, "matrix": [[float(x) for x in row] for row in corr]},
            "live": None,
            "note": f"common window of the 4 reference series: {len(days)} days"}


async def portfolio(db=None) -> dict:
    base = _pcache["v"]
    if base is None or time.time() - _pcache["t"] > PORTFOLIO_TTL:
        try:
            base = await asyncio.to_thread(compute_portfolio)
        except Exception as e:  # noqa: BLE001
            base = _empty(f"portfolio computation failed: {type(e).__name__}")
        _pcache.update(t=time.time(), v=base)
    live = await asyncio.to_thread(lambda: _live_stats(_series(db, "sleeve"), "total"))
    return {**base, "live": {"days": live["days"], "return_pct": live["return_pct"], "sharpe": live["sharpe"]}
            if live else None}


# ---- ML --------------------------------------------------------------------------------
def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def ml_view(db=None) -> dict:
    models = []
    try:
        rows = db.models(50) if db is not None else []
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        m = _json(r.get("metrics"))
        m = m if isinstance(m, dict) else {}
        auc = next((_f(m[k]) for k in ("auc", "oos_auc", "test_auc", "wf_auc", "mean_auc") if k in m), None)
        models.append({"id": r.get("id"), "created_at": r.get("created_at"), "status": r.get("status"),
                       "kind": "entry", "auc": auc, "metrics": m})
    opt = None
    try:
        from . import options_ml
        mdl = options_ml.load_model()
        if mdl:
            opt = {k: v for k, v in mdl.items() if k not in ("weights", "features")
                   and (not isinstance(v, (list, dict)) or k == "metrics")}
    except Exception:  # noqa: BLE001
        opt = None
    exps = [{**e, "evidence": _evidence(e["evidence"])} for e in PIPELINE if e["kind"] == "ml"]
    champ = next((m for m in models if m["status"] == "champion"), None)
    if champ:
        summary = f"{len(models)} entry models on record; the champion is model {champ['id']}."
    elif models:
        summary = f"{len(models)} entry models on record, none is champion."
    else:
        summary = "No entry models are on record yet."
    return {"models": models, "options_model": opt, "experiments": exps, "summary": summary}
