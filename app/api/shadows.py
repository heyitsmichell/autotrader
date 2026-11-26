"""Shadow strategy endpoints (docs/SHADOWS.md): virtual money, no orders."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..shadows import registry
from .deps import engine

router = APIRouter()


def _manager(request: Request):
    eng = engine(request)
    sm = getattr(eng, "shadows", None)
    if sm is None:
        from ..shadows.manager import ShadowManager
        sm = eng.shadows = ShadowManager(eng)
    return sm


def _check(shadow_id: str) -> None:
    try:
        registry.get(shadow_id)
    except KeyError as e:
        raise HTTPException(404, f"unknown shadow {shadow_id!r}") from e


@router.get("/api/shadows")
def shadows(request: Request):
    return _manager(request).summary()


@router.get("/api/shadows/equity")
def shadows_equity(request: Request, days: int = 365):
    return _manager(request).equity_series(max(1, min(int(days), 3650)))


@router.get("/api/shadows/{shadow_id}")
def shadow_detail(request: Request, shadow_id: str):
    _check(shadow_id)
    return _manager(request).detail(shadow_id)


@router.post("/api/shadows/{shadow_id}/reset")
async def shadow_reset(request: Request, shadow_id: str):
    _check(shadow_id)
    _manager(request).reset(shadow_id)
    return {"ok": True}
