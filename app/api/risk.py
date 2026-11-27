"""Read-only risk report endpoints (docs/RISK_TAB_PLAN.md). No broker writes, ever."""
from __future__ import annotations

import asyncio
import logging
import time

import pandas as pd
from fastapi import APIRouter, Request

from ..market import now_utc
from ..risk_report import inputs, report, scenarios
from ..risk_report.pricing import RATE
from ..sleeves import data as sleeve_data
from .deps import engine

log = logging.getLogger("trader.risk_report")
router = APIRouter()

CACHE_TTL = 15 * 60
REFRESH_MIN_GAP = 60
METHOD = "Historical simulation on today's holdings; options repriced with Black-Scholes."


def _cache(request: Request) -> dict:
    st = request.app.state
    if not hasattr(st, "risk_cache"):
        st.risk_cache = {}
    return st.risk_cache


def _closes_sync(symbols: list[str], warnings: list[str]) -> pd.DataFrame:
    try:
        df = sleeve_data.daily_closes(sorted(set(symbols)), "18mo")
        return df.iloc[-(report.LOOKBACK + 1):] if df is not None and not df.empty else pd.DataFrame()
    except Exception as exc:  # noqa: BLE001
        log.warning("risk: price history failed: %s", exc)
        warnings.append("price history unavailable")
        return pd.DataFrame()


async def _load(eng, options_only: bool):
    inp = await inputs.gather(eng)
    warnings = inp["warnings"]
    hs = [h for h in inp["holdings"] if h["kind"] == "option"] if options_only else inp["holdings"]
    syms = {report.under(h) for h in hs}
    if not options_only:
        syms.add("SPY")
    closes = await asyncio.to_thread(_closes_sync, list(syms), warnings) if syms else pd.DataFrame()
    return inp, report.prepare(hs, closes, warnings), closes, warnings


def _lock(request: Request, key: str) -> asyncio.Lock:
    st = request.app.state
    if not hasattr(st, "risk_locks"):
        st.risk_locks = {}
    return st.risk_locks.setdefault(key, asyncio.Lock())


def _fresh(c: dict, key: str, refresh: bool):
    hit = c.get(key)
    if hit:
        age = time.time() - hit["ts"]
        if age < CACHE_TTL and not (refresh and age >= REFRESH_MIN_GAP):
            return {**hit["payload"], "cached": True}
    return None


def _skeleton_risk() -> dict:
    return {"lookback_days": 0,
            "account": {k: None for k in ("equity", "var95", "var99", "es95", "es99", "var95_pct", "vol_ann_pct",
                                          "beta_spy", "gross_exposure", "net_exposure", "delta_dollars")},
            "sleeves": [], "holdings": [], "stress": [], "drawdowns": {},
            "correlation": {"ids": [], "holdings": [], "live": None, "live_days": 0},
            "concentration": {"top5_pct": None, "shared_symbols": []}}


def _skeleton_options() -> dict:
    return {"rate": RATE, "positions": [],
            "grid": {"spot_pct": scenarios.SPOT_PCT, "iv_shift_pct": scenarios.IV_SHIFT_PCT,
                     "horizons": scenarios.HORIZONS, "pnl": {}, "total": scenarios.total_grid([])}}


async def _cached(request: Request, key: str, refresh: bool, compute, skeleton=_skeleton_risk):
    c = _cache(request)
    out = _fresh(c, key, refresh)
    if out:
        return out
    async with _lock(request, key):
        out = _fresh(c, key, refresh)          # another request may have filled it while we waited
        if out:
            return out
        return await _compute(c, key, compute, skeleton)


ERR_TTL = 30
COMPUTE_TIMEOUT = 60


async def _compute(c: dict, key: str, compute, skeleton):
    now = time.time()
    hit = c.get(key)
    err = c.get(key + ":err")
    if not hit and err and now - err["ts"] < ERR_TTL:
        return {**err["payload"], "cached": True}
    try:
        payload = await asyncio.wait_for(compute(), COMPUTE_TIMEOUT)
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 - degrade, never 500
        exc = exc or "timed out"
        log.exception("risk: %s compute failed", key)
        if hit:
            return {**hit["payload"], "cached": True,
                    "warnings": [*hit["payload"].get("warnings", []), f"refresh failed: {exc}"]}
        body = {**skeleton(), "as_of": now_utc().isoformat(timespec="seconds"), "method": METHOD,
                "warnings": [f"risk report unavailable: {exc}"], "error": True}
        c[key + ":err"] = {"ts": now, "payload": body}
        return {**body, "cached": False}
    c[key] = {"ts": now, "payload": payload}
    return {**payload, "cached": False}


@router.get("/api/risk")
async def risk(request: Request, refresh: int = 0):
    eng = engine(request)

    async def compute():
        inp, hs, closes, warnings = await _load(eng, False)
        body = report.build_report(hs, closes, inp["sleeves"], inp["net_liq"], inp["series"], warnings)
        return {"as_of": now_utc().isoformat(timespec="seconds"), "lookback_days": body.pop("lookback_days"),
                "method": METHOD, **body}

    return await _cached(request, "risk", bool(refresh), compute)


@router.get("/api/risk/options")
async def risk_options(request: Request, refresh: int = 0):
    eng = engine(request)

    async def compute():
        inp, hs, _closes, warnings = await _load(eng, True)
        positions, grids = [], {}
        for h in hs:
            p = {"symbol": h["symbol"], "underlying": h["underlying"], "sleeve": h["sleeve"], "right": h["right"],
                 "strike": h["strike"], "expiry": h["expiry"], "dte": h["dte"], "qty": h["qty"],
                 "mark": report._r(h.get("mark"), 4), "iv": report._r(h["iv"], 4), "iv_source": h.get("iv_source"),
                 "spot": report._r(h["spot"], 2)}
            p.update(scenarios.position_stats({**h, "mark": h.get("mark")},
                                              (inp["sleeves"].get(h["sleeve"]) or {}).get("cash")))
            positions.append(p)
            grids.setdefault(h["symbol"], []).append(scenarios.position_grid(h))
        # same OCC symbol held by two sleeves: sum the grids under one key
        merged = {k: scenarios.total_grid(gs) for k, gs in grids.items()}
        return {"as_of": now_utc().isoformat(timespec="seconds"), "rate": RATE, "method": METHOD, "warnings": warnings,
                "positions": positions,
                "grid": {"spot_pct": scenarios.SPOT_PCT, "iv_shift_pct": scenarios.IV_SHIFT_PCT,
                         "horizons": scenarios.HORIZONS, "pnl": merged,
                         "total": scenarios.total_grid([g for gs in grids.values() for g in gs])}}

    return await _cached(request, "options", bool(refresh), compute, _skeleton_options)
