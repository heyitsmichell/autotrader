"""Backtests tab API (docs/BACKTESTS_TAB.md)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from .. import backtests_view as bv

router = APIRouter()


def _db(request: Request):
    return getattr(request.app.state, "db", None)


@router.get("/api/backtests/strategies")
async def strategies(request: Request):
    return await asyncio.to_thread(bv.strategies, _db(request))


@router.get("/api/backtests/portfolio")
async def portfolio(request: Request):
    return await bv.portfolio(_db(request))


@router.get("/api/backtests/pipeline")
async def pipeline(request: Request):
    return await asyncio.to_thread(bv.pipeline_view, _db(request))


@router.get("/api/backtests/ml")
async def ml(request: Request):
    return await asyncio.to_thread(bv.ml_view, _db(request))
