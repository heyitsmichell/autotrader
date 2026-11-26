"""Proposal list/detail and approve / requote / dismiss actions."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from ..db import decode
from .deps import engine
from .models import ProposalOut

router = APIRouter()


def list_proposals(st, limit: int = 50, symbol: str | None = None) -> list[dict]:
    """Same list GET /api/proposals returns (the SSE `proposals` payload)."""
    rows = st.db.proposals(max(1, min(int(limit), 500)), symbol.upper() if symbol else None)
    return [decode(r) for r in rows]


@router.get("/api/proposals", response_model=list[ProposalOut])
def proposals(request: Request, limit: int = 50, symbol: str | None = None):
    return list_proposals(request.app.state, limit, symbol)


@router.get("/api/proposals/{pid}")
def proposal_detail(request: Request, pid: int):
    db = request.app.state.db
    p = decode(db.get_proposal(pid))
    if p is None:
        raise HTTPException(404, f"proposal #{pid} not found")
    sig = decode(db.get_signal(p["signal_id"])) if p.get("signal_id") else None
    # The headlines that were inside the sentiment window when the proposal was made.
    made = datetime.fromisoformat(p["created_at"])
    lookback = timedelta(hours=request.app.state.settings.news_lookback_hours)
    headlines = db.news(p["symbol"], (made - lookback).isoformat(timespec="seconds"), 8,
                        until=p["created_at"])
    protective = [o for o in db.protective(p["symbol"], open_only=False)
                  if o["proposal_id"] == pid]
    return {"proposal": p, "signal": sig, "headlines": headlines, "protective": protective}


@router.post("/api/proposals/{pid}/approve")
async def approve(request: Request, pid: int):
    try:
        return decode(await engine(request).execute(pid, actor="user"))
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/api/proposals/{pid}/requote")
async def requote(request: Request, pid: int):
    """Rebuild a stale pending proposal at the current price. Never places an order."""
    try:
        return await engine(request).requote(pid)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/api/proposals/{pid}/dismiss")
async def dismiss(request: Request, pid: int):
    try:
        engine(request).dismiss(pid)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}
