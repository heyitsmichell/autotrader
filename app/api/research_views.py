"""Read-only per-strategy Research views (docs/RESEARCH_TAB.md). Never places orders."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import research_views as rv
from .deps import engine

router = APIRouter()


async def _safe(aw):
    try:
        return await aw
    except Exception as e:  # noqa: BLE001 - a data problem is a message, never a 500
        rv.log.warning("research view failed: %s", e, exc_info=True)
        return rv._err("This view could not be built right now. Try again in a minute.")


@router.get("/api/research/momentum")
async def research_momentum(request: Request):
    st = request.app.state
    return await _safe(rv.build_momentum(st.db, st.settings))


@router.get("/api/research/vaa")
async def research_vaa(request: Request):
    return await _safe(rv.build_vaa(request.app.state.db))


@router.get("/api/research/putwrite")
async def research_putwrite(request: Request):
    st = request.app.state
    return await _safe(rv.build_putwrite(st.db, engine(request).broker, st.settings))


@router.get("/api/research/overlap")
async def research_overlap(request: Request):
    st, eng = request.app.state, engine(request)
    return await _safe(rv.build_overlap(st.db, eng.broker,
                                        getattr(eng, "portfolio", None) or getattr(st, "portfolio", None)))


@router.get("/api/research/regime")
async def research_regime(request: Request):
    return await _safe(rv.build_regime(request.app.state.db))
