"""Pure analytics for the sleeves (docs/SLEEVES_ARCHITECTURE.md section 8). All returns are fractions."""
from __future__ import annotations

import csv
import gzip
import json
import math
from pathlib import Path

REF_DIR = Path(__file__).resolve().parent / "reference"
HORIZON = 252
_BANDS: dict | None = None


def bands() -> dict:
    global _BANDS
    if _BANDS is None:
        try:
            _BANDS = json.loads((REF_DIR / "bands.json").read_text())
        except (OSError, ValueError):
            _BANDS = {}
    return _BANDS


def returns_from_equity(points: list) -> list[tuple[str, float]]:
    out, prev = [], None
    for day, eq in points:
        if eq is None:
            continue
        if prev:
            out.append((day, eq / prev - 1))
        prev = eq
    return out


def drawdown_series(rets: list) -> list[tuple[str, float]]:
    eq = peak = 1.0
    out = []
    for day, r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        out.append((day, eq / peak - 1))
    return out


def worst_drawdowns(rets: list, n: int = 5) -> list[dict]:
    dd = drawdown_series(rets)
    eps: list[dict] = []
    cur = None
    for i, (day, d) in enumerate(dd):
        if d < 0:
            if cur is None:        # the episode starts at the last day at the peak (the day before)
                cur = {"si": max(i - 1, 0), "trough": day, "depth": d, "end": None, "ei": None}
            elif d < cur["depth"]:
                cur.update(trough=day, depth=d)
        elif cur is not None:
            cur.update(end=day, ei=i)
            eps.append(cur)
            cur = None
    if cur is not None:
        eps.append(cur)
    last = len(dd) - 1
    out = [{"start": dd[e["si"]][0], "trough": e["trough"], "end": e["end"], "depth": e["depth"],
            "days": (e["ei"] if e["ei"] is not None else last) - e["si"]} for e in eps]
    out.sort(key=lambda e: e["depth"])
    return out[:n]


def monthly_returns(rets: list) -> list[dict]:
    acc: dict[tuple[int, int], float] = {}
    for day, r in rets:
        k = (int(day[:4]), int(day[5:7]))
        acc[k] = acc.get(k, 1.0) * (1 + r)
    return [{"year": y, "month": m, "ret": v - 1} for (y, m), v in sorted(acc.items())]


def yearly_returns(rets: list) -> list[dict]:
    acc: dict[int, float] = {}
    for day, r in rets:
        acc[int(day[:4])] = acc.get(int(day[:4]), 1.0) * (1 + r)
    return [{"year": y, "ret": v - 1} for y, v in sorted(acc.items())]


def _mean_std(x: list[float]) -> tuple[float, float]:
    m = sum(x) / len(x)
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1)) if len(x) > 1 else 0.0
    return m, sd


def rolling_sharpe(rets: list, window: int = 126) -> list[tuple[str, float]]:
    out = []
    for i in range(window - 1, len(rets)):
        w = [r for _, r in rets[i - window + 1:i + 1]]
        m, sd = _mean_std(w)
        if sd > 0:
            out.append((rets[i][0], m / sd * math.sqrt(252)))
    return out


def metrics(rets: list) -> dict:
    keys = ("cagr", "vol", "sharpe", "sortino", "max_dd", "calmar", "best_day", "worst_day", "pct_positive_days")
    if len(rets) < 20:
        return {k: None for k in keys}
    x = [r for _, r in rets]
    m, sd = _mean_std(x)
    total = 1.0
    for r in x:
        total *= 1 + r
    cagr = total ** (252 / len(x)) - 1 if total > 0 else -1.0
    dsd = math.sqrt(sum(min(r, 0.0) ** 2 for r in x) / len(x))
    mdd = min(d for _, d in drawdown_series(rets))
    return {
        "cagr": cagr, "vol": sd * math.sqrt(252),
        "sharpe": m / sd * math.sqrt(252) if sd > 0 else None,
        "sortino": m / dsd * math.sqrt(252) if dsd > 0 else None,
        "max_dd": mdd, "calmar": cagr / abs(mdd) if mdd < 0 else None,
        "best_day": max(x), "worst_day": min(x), "pct_positive_days": sum(1 for r in x if r > 0) / len(x),
    }


def band_for(sleeve: str, live_points: list) -> dict:
    """Live cumulative return vs the bootstrap band; the band index is trading days since the start."""
    pts = [(d, e) for d, e in live_points if e is not None]
    ref = bands().get(sleeve) or {}
    out = {"sleeve": sleeve, "start": pts[0][0] if pts else None, "window": ref.get("window"),
           "status": "insufficient", "below_days": 0, "points": []}
    if not pts or not ref:
        return out
    base = pts[0][1]
    for i, (day, eq) in enumerate(pts):
        h = min(i, HORIZON)
        row = {"day": day, "live": eq / base - 1 if base else 0.0}
        for q in ("p5", "p25", "p50", "p75", "p95"):
            row[q] = 0.0 if h == 0 else ref[q][h - 1]
        out["points"].append(row)
    out["status"], out["below_days"] = drift_status(out)
    return out


def drift_status(band: dict) -> tuple[str, int]:
    pts = band.get("points") or []
    if len(pts) < 5:
        return "insufficient", 0
    last = pts[-1]
    if last["live"] < last["p5"]:
        n = 0
        for p in reversed(pts):
            if p["live"] >= p["p5"]:
                break
            n += 1
        return "below", n
    if last["live"] > last["p95"]:
        return "above", 0
    return "inside", 0


def load_reference_returns(sleeve: str) -> list[tuple[str, float]]:
    with gzip.open(REF_DIR / f"{sleeve}.csv.gz", "rt", newline="") as f:
        return [(r["date"], float(r["ret"])) for r in csv.DictReader(f)]


def tearsheet(sleeve: str, rets: list, source: str, window: int = 126) -> dict:
    return {
        "sleeve": sleeve, "source": source, "days": len(rets),
        "start": rets[0][0] if rets else None, "end": rets[-1][0] if rets else None,
        "metrics": metrics(rets),
        "drawdown": [{"day": d, "dd": v} for d, v in drawdown_series(rets)],
        "rolling_sharpe": [{"day": d, "value": v} for d, v in rolling_sharpe(rets, window)],
        "monthly": monthly_returns(rets), "yearly": yearly_returns(rets),
        "worst_drawdowns": worst_drawdowns(rets, 5),
    }
