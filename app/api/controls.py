"""Operator controls: run, mode, scheduler, kill, flatten, alerts, shutdown, broker reconnect."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..broker import BrokerError
from .deps import background, engine
from .models import FlattenIn, KillIn, ModeIn, ToggleIn

router = APIRouter()


@router.post("/api/run")
async def run_now(request: Request):
    eng = engine(request)
    if not request.app.state.broker.connected:
        raise HTTPException(409, "Broker is not connected")
    if eng.kill_switch:
        raise HTTPException(409, "Kill switch is on")
    if eng.running:
        raise HTTPException(409, "A run is already in progress")
    background(request, eng.run_cycle("manual"))
    return {"started": True}


@router.post("/api/mode")
async def set_mode(request: Request, body: ModeIn):
    st = request.app.state
    if body.mode == "auto" and st.settings.is_live and body.confirm != "AUTO":
        raise HTTPException(400, 'Switching a live account to auto mode needs {"confirm": "AUTO"}')
    try:
        engine(request).set_mode(body.mode)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.post("/api/scheduler")
async def scheduler(request: Request, body: ToggleIn):
    eng = engine(request)
    if body.on:
        try:
            eng.start_scheduler()
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
    else:
        eng.scheduler_stopped_by_user = True   # the supervisor won't restart it
        await eng.stop_scheduler()
    return {"ok": True}


@router.post("/api/kill")
async def kill(request: Request, body: KillIn):
    eng = engine(request)
    if body.on:
        await eng.set_kill_switch(True, "turned on by you")
    else:
        if body.confirm != "RELEASE":
            raise HTTPException(400, 'Releasing the kill switch needs {"on": false, "confirm": "RELEASE"}')
        if eng.flattening:
            raise HTTPException(409, "Wait for the flatten to finish")
        request.app.state.supervisor.on_release()
        await eng.set_kill_switch(False)
    return {"ok": True}


@router.post("/api/flatten")
async def flatten(request: Request, body: FlattenIn):
    """Kill switch + cancel every open order + sell every position. Needs
    {"confirm": "FLATTEN"} so it can't be triggered by accident."""
    if body.confirm != "FLATTEN":
        raise HTTPException(400, 'Send {"confirm": "FLATTEN"} to confirm')
    if not request.app.state.broker.connected:
        raise HTTPException(409, "Broker is not connected")
    try:
        return await engine(request).flatten("user", "requested by you")
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/api/alerts/test")
async def alerts_test(request: Request):
    alerts = request.app.state.alerts
    if not alerts.channels:
        raise HTTPException(409, "No alert channel is set up. Add ALERT_NTFY_TOPIC (or another "
                                 "channel) to .env and restart.")
    results = await alerts.send("Test alert: if you can read this, alerts work.", "info",
                                "Trader test alert")
    return {"results": results}


@router.get("/api/alerts/summary")
async def alerts_summary(request: Request):
    return {"text": await request.app.state.supervisor.summary_text()}


@router.post("/api/shutdown")
async def shutdown(request: Request):
    """Stop the app cleanly (used by scripts/trader.py). Runs in progress are let finish
    their current step; orders at Webull are not touched."""
    server = getattr(request.app.state, "server", None)
    if server is None:
        raise HTTPException(409, "Not started through app.main; stop it with Ctrl+C")
    request.app.state.db.log("info", "Shutdown requested")
    server.should_exit = True
    return {"ok": True}


@router.post("/api/broker/reconnect")
async def reconnect(request: Request):
    broker, db = request.app.state.broker, request.app.state.db
    if engine(request).running:
        raise HTTPException(409, "Wait for the current run to finish")
    try:
        await broker.start()
    except BrokerError as e:
        db.log("error", str(e))
        raise HTTPException(502, str(e)) from e
    request.app.state.portfolio.invalidate()
    db.log("info", f"Reconnected to MCP server ({len(broker.tools)} tools)")
    return {"ok": True}
