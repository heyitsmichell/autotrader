"""Results tab endpoints (docs/RESULTS.md): read-only, current namespace only."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import results

router = APIRouter()


@router.get("/api/results/trades")
def results_trades(request: Request, strategy: str = "all", days: int = 90,
                         include_failed: int = 0, limit: int = 500):
    if strategy not in results.STRATEGIES:
        raise HTTPException(400, f"unknown strategy {strategy!r}")
    if not 1 <= days <= 3650:
        raise HTTPException(400, "days must be 1..3650")
    if not 1 <= limit <= 2000:
        raise HTTPException(400, "limit must be 1..2000")
    return results.trades(request.app.state.db, strategy, days, bool(include_failed), limit)


@router.get("/api/results/pnl")
def results_pnl(request: Request, days: int = 90):
    if not 1 <= days <= 3650:
        raise HTTPException(400, "days must be 1..3650")
    return results.pnl(request.app.state.db, days)


@router.get("/api/results/round_trips")
def results_round_trips(request: Request, days: int = 365):
    if not 1 <= days <= 3650:
        raise HTTPException(400, "days must be 1..3650")
    return results.round_trips(request.app.state.db, days)
