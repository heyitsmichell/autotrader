"""Multi-strategy sleeve endpoints (docs/SLEEVES_ARCHITECTURE.md section 6)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..sleeves import analytics, registry
from .deps import engine
from .models import ToggleIn

router = APIRouter()


def _manager(request: Request):
    eng = engine(request)
    sm = getattr(eng, "sleeves", None)
    if sm is None:
        from ..sleeves.manager import SleeveManager
        sm = eng.sleeves = SleeveManager(eng)
    return sm


def _check(sleeve_id: str) -> None:
    try:
        registry.get(sleeve_id)
    except KeyError as e:
        raise HTTPException(404, f"unknown sleeve {sleeve_id!r}") from e


@router.get("/api/sleeves")
async def sleeves(request: Request):
    return await _manager(request).summary()


@router.get("/api/sleeves/equity")
def sleeves_equity(request: Request, days: int = 365):
    return _manager(request).equity_series(max(1, min(int(days), 3650)))


@router.get("/api/sleeves/events")
def sleeve_events(request: Request, days: int = 365, sleeve: str | None = None):
    if sleeve:
        _check(sleeve)
    rows = _manager(request).db.sleeve_events(max(1, min(int(days), 3650)), sleeve or None)
    return {"events": rows}


@router.get("/api/sleeves/{sleeve_id}/band")
def sleeve_band(request: Request, sleeve_id: str):
    _check(sleeve_id)
    series = _manager(request).db.sleeve_equity_series(3650)
    band = analytics.band_for(sleeve_id, [(r["day"], r.get(sleeve_id)) for r in series])
    return band


@router.get("/api/sleeves/{sleeve_id}/tearsheet")
def sleeve_tearsheet(request: Request, sleeve_id: str, source: str = "live", window: int = 126):
    _check(sleeve_id)
    if source not in ("live", "backtest"):
        raise HTTPException(400, "source must be 'live' or 'backtest'")
    window = max(5, min(int(window), 756))
    if source == "live":
        series = _manager(request).db.sleeve_equity_series(3650)
        rets = analytics.returns_from_equity([(r["day"], r.get(sleeve_id)) for r in series])
    else:
        rets = analytics.load_reference_returns(sleeve_id)
    return analytics.tearsheet(sleeve_id, rets, source, window)


@router.get("/api/sleeves/{sleeve_id}")
async def sleeve_detail(request: Request, sleeve_id: str):
    _check(sleeve_id)
    return await _manager(request).detail(sleeve_id)


@router.post("/api/sleeves/{sleeve_id}/pause")
async def sleeve_pause(request: Request, sleeve_id: str, body: ToggleIn):
    _check(sleeve_id)
    await _manager(request).set_paused(sleeve_id, bool(body.on),
                                       "paused from the dashboard" if body.on else "resumed from the dashboard")
    return {"ok": True}


@router.post("/api/sleeves/{sleeve_id}/rebalance")
async def sleeve_rebalance(request: Request, sleeve_id: str):
    _check(sleeve_id)
    if not request.app.state.settings.sleeves_enabled:
        raise HTTPException(409, "Multi-strategy mode is off (SLEEVES_ENABLED=false)")
    try:
        msg = await _manager(request).rebalance_now(sleeve_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "message": msg}
