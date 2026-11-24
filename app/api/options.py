"""Options endpoints (chain, snapshot, status)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..broker import BrokerError

router = APIRouter()


@router.get("/api/options/chain")
async def option_chain(request: Request, underlying: str, min_dte: int | None = None, max_dte: int | None = None):
    """Get option chain for an underlying."""
    st = request.app.state
    if not st.settings.options_enabled:
        raise HTTPException(403, "Options trading is disabled (OPTIONS_ENABLED=false)")
    if not st.broker.connected:
        raise HTTPException(409, "Broker is not connected")
    underlying = underlying.upper()
    if underlying not in st.settings.options_allowed_underlyings:
        raise HTTPException(400, f"{underlying} not in OPTIONS_ALLOWED_UNDERLYINGS")
    min_dte = min_dte or st.settings.options_min_dte
    max_dte = max_dte or st.settings.options_max_dte
    try:
        result = await st.broker.option_contracts([underlying])
        # Parse the text response from Webull MCP / mock server
        contracts = _parse_option_chain_text(result, min_dte, max_dte)
        return {"underlying": underlying, "contracts": contracts, "raw": result}
    except BrokerError as e:
        raise HTTPException(400, str(e)) from e



def _parse_option_chain_text(text: str, min_dte: int, max_dte: int) -> list[dict]:
    """Parse the text format option chain from Webull MCP or mock server."""
    import re
    from datetime import datetime, timezone
    contracts = []

    # Try mock server format first (compact single-line)
    # Format: "SYMBOL  Type: CALL/PUT  Strike: XX.XX  Exp: YYYY-MM-DD  DTE: N  Bid: X.XX  Ask: X.XX  Vol: N  OI: N"
    pattern = r'(\w+)\s+Type:\s+(CALL|PUT)\s+Strike:\s+([\d.]+)\s+Exp:\s+(\d{4}-\d{2}-\d{2})\s+DTE:\s+(\d+)\s+Bid:\s+([\d.]+)\s+Ask:\s+([\d.]+)\s+Vol:\s+(\d+)\s+OI:\s+(\d+)'
    matches = list(re.finditer(pattern, text))
    if matches:
        for match in matches:
            symbol, opt_type, strike, exp, dte, bid, ask, vol, oi = match.groups()
            dte_int = int(dte)
            if min_dte <= dte_int <= max_dte:
                contracts.append({
                    "symbol": symbol,
                    "type": opt_type,
                    "strike": float(strike),
                    "expiration": exp,
                    "dte": dte_int,
                    "bid": float(bid),
                    "ask": float(ask),
                    "last": (float(bid) + float(ask)) / 2,
                    "volume": int(vol),
                    "open_interest": int(oi),
                    "iv": 0.0,
                    "delta": 0.0,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                })
        return contracts

    # Try Webull MCP format (multi-line structured)
    # Each contract block starts with "  Symbol: OCC_SYMBOL" then has indented lines
    # with "Strike:", "Expiration:", "Type:", etc.
    symbol_pattern = r'^\s{2}Symbol:\s+(\w+)'
    for match in re.finditer(symbol_pattern, text, re.MULTILINE):
        symbol = match.group(1)
        # Extract the block for this symbol (until next "  Symbol:" or end)
        block_start = match.end()
        next_symbol = re.search(r'^\s{2}Symbol:', text[block_start:], re.MULTILINE)
        block_end = block_start + next_symbol.start() if next_symbol else len(text)
        block = text[block_start:block_end]

        # Parse fields from the block
        strike_match = re.search(r'Strike:\s+([\d.]+)', block)
        exp_match = re.search(r'Expiration:\s+(\d{4}-\d{2}-\d{2})', block)
        type_match = re.search(r'Type:\s+(CALL|PUT)', block)

        if strike_match and exp_match and type_match:
            strike = float(strike_match.group(1))
            exp = exp_match.group(1)
            opt_type = type_match.group(1)

            # Calculate DTE
            try:
                exp_dt = datetime.fromisoformat(exp).replace(tzinfo=timezone.utc)
                dte = (exp_dt - datetime.now(timezone.utc)).days
            except Exception:
                dte = 0

            if min_dte <= dte <= max_dte:
                contracts.append({
                    "symbol": symbol,
                    "type": opt_type,
                    "strike": strike,
                    "expiration": exp,
                    "dte": dte,
                    "bid": 0.0,
                    "ask": 0.0,
                    "last": 0.0,
                    "volume": 0,
                    "open_interest": 0,
                    "iv": 0.0,
                    "delta": 0.0,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                })

    return contracts


@router.get("/api/options/snapshot")
async def option_snapshot(request: Request, symbols: str):
    """Get option snapshot (Greeks, IV, liquidity) for OCC symbols."""
    st = request.app.state
    if not st.settings.options_enabled:
        raise HTTPException(403, "Options trading is disabled (OPTIONS_ENABLED=false)")
    if not st.broker.connected:
        raise HTTPException(409, "Broker is not connected")
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    try:
        result = await st.broker.parsed_option_snapshots(sym_list)
        return result
    except BrokerError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/options/status")
async def options_status(request: Request):
    """Options feature status and configuration."""
    st = request.app.state
    return {
        "enabled": st.settings.options_enabled,
        "broker_supports_options": st.broker.connected and hasattr(st.broker, "option_contracts"),
        "allowed_strategies": st.settings.options_allowed_strategies,
        "allowed_underlyings": st.settings.options_allowed_underlyings,
        "max_contracts_per_order": st.settings.options_max_contracts_per_order,
        "max_notional_usd": st.settings.options_max_notional_usd,
        "min_dte": st.settings.options_min_dte,
        "max_dte": st.settings.options_max_dte,
        "require_approval": st.settings.options_require_approval,
        "consensus_gate": {
            "iv_rank_max": st.settings.options_iv_rank_max,
            "iv_rank_min": st.settings.options_iv_rank_min,
            "delta_min": st.settings.options_delta_min,
            "delta_max": st.settings.options_delta_max,
            "min_volume": st.settings.options_min_volume,
            "min_open_interest": st.settings.options_min_open_interest,
            "max_bid_ask_pct": st.settings.options_max_bid_ask_pct,
        },
        "broker_label": st.settings.broker_label,
        "is_live": st.settings.is_live,
    }
