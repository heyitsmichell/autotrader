"""Chart history and overlay endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..broker import BrokerError, bar_day, parse_bars

router = APIRouter()

TIMEFRAMES = {"1m": ("M1", 390), "5m": ("M5", 390), "15m": ("M15", 300),
              "1h": ("M60", 300), "1D": ("D", 300)}
# Extended hours (pre-market 4:00-9:30 + regular 9:30-16:00 + after-hours 16:00-20:00 = up to 960 minutes)
TIMEFRAMES_EXTENDED = {"1m": ("M1", 960), "5m": ("M5", 400), "15m": ("M15", 300),
                       "1h": ("M60", 300), "1D": ("D", 300)}


def chart_symbol(request: Request, symbol: str) -> str:
    sym = symbol.strip().upper()
    if sym not in request.app.state.settings.watchlist:
        raise HTTPException(404, f"{sym} is not on the watchlist")
    return sym


@router.get("/api/chart/history")
async def chart_history(request: Request, symbol: str, tf: str = "1m", extended: bool = True):
    sym = chart_symbol(request, symbol)
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"tf must be one of {list(TIMEFRAMES)}")
    timespan, count = TIMEFRAMES_EXTENDED[tf] if extended else TIMEFRAMES[tf]
    kwargs = {
        "symbols": sym,
        "timespan": timespan,
        "count": count,
        "real_time_required": True,
    }
    if extended and tf != "1D":
        # Pass trading_sessions to include pre-market, regular, and after-hours
        kwargs["trading_sessions"] = "PRE,RTH,ATH"
    try:
        text = await request.app.state.broker.call("get_stock_bars", **kwargs)
    except BrokerError as e:
        raise HTTPException(502, str(e)) from e
    bars = [{
        # Daily bars use the trading date; intraday bars use UTC seconds (shown in ET).
        "time": bar_day(b["time"]).isoformat() if tf == "1D" else int(b["time"].timestamp()),
        "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
        "volume": b["volume"],
    } for b in parse_bars(text)]
    return {"symbol": sym, "tf": tf, "extended": extended, "bars": bars}


@router.get("/api/chart/overlays")
async def chart_overlays(request: Request, symbol: str):
    """Proposal markers and price lines (cost basis, pending limits, stops, targets)."""
    st = request.app.state
    sym = chart_symbol(request, symbol)
    markers = [{
        "id": p["id"], "time": p["created_at"], "side": p["side"], "status": p["status"],
        "price": p["limit_price"], "quantity": p["quantity"],
    } for p in st.db.proposals(200, sym)]
    lines = []
    for p in st.db.proposals(50, sym):
        if p["status"] == "pending":
            lines.append({"kind": "pending", "price": p["limit_price"],
                          "label": f"#{p['id']} {p['side']} limit"})
    for o in st.db.protective(sym):
        lines.append({"kind": o["kind"], "price": o["price"], "status": o["status"],
                      "label": ("Stop" if o["kind"] == "sl" else "Target")
                      + (" (armed on fill)" if o["status"] == "waiting" else "")})
    if st.broker.connected and st.portfolio.cached:
        for pos in st.portfolio.cached["positions"]:
            if pos["symbol"] == sym and pos.get("cost"):
                lines.append({"kind": "cost", "price": pos["cost"],
                              "label": f"Avg cost ({pos['qty']:g} sh)"})
    return {"symbol": sym, "markers": markers, "lines": lines}
