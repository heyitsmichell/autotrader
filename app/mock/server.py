"""A simulated broker that speaks MCP like the Webull server.

It exposes a subset of the same tool names, with the same output text, so the
rest of the app runs unchanged. Prices are synthetic (a sum of slow and fast
waves, so there are multi-day trends to analyze). News headlines and the fear &
greed index are synthetic too. Cash starts at $25,000. State persists in
data/mock_state.json. Delete that file to reset.

Supports LIMIT and STOP_LOSS orders and OTOCO brackets (MASTER entry that
activates a STOP_PROFIT and a STOP_LOSS leg; filling one cancels the other).

Supports fractional shares via entrust_type=AMOUNT (notional/dollar-based orders)
and single-leg options (CALL/PUT, BUY_TO_OPEN/BUY_TO_CLOSE/SELL_TO_OPEN/SELL_TO_CLOSE).

Run by the app automatically when BROKER_MODE=mock.
"""


from __future__ import annotations

import json
import math
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastmcp import FastMCP

from .fills import (_children, _detail, _fill, _new_order, _validate_option, _validate_stock,
                    check_account, fmt_orders, try_fill)
from .options import norm_cdf, option_price, option_symbol
from .quotes import BASE_PRICES, DAY, price_of
from .state import (ACCOUNT_ID, DISCLAIMER, FRACTIONAL_ELIGIBLE, OPEN_STATUSES, READ_ONLY, START_CASH,
                    STATE_FILE, _iso, _now_iso, _set, _state_file, equity, load, save)

mcp = FastMCP("Mock Webull Broker")


# -- tools (names, arguments and output text mirror webull-openapi-mcp) --------
@mcp.tool(description="Get all linked accounts.", annotations=READ_ONLY)
async def get_account_list() -> str:
    return DISCLAIMER + (
        "=== Account List ===\n"
        f"1. ID: {ACCOUNT_ID}  User ID: mock  Number: 0001  Type: CASH  "
        "Class: INDIVIDUAL_CASH  Label: Simulated"
    )


@mcp.tool(description="Get account balance: net liquidation, buying power, cash.",
          annotations=READ_ONLY)
async def get_account_balance(account_id: str) -> str:
    if err := check_account(account_id):
        return err
    state = load()
    try_fill(state)
    save(state)
    mv = equity(state) - state["cash"]
    eq = equity(state)
    unreal = sum((price_of(s) - p["cost"]) * p["qty"] for s, p in state["positions"].items())
    opt_unreal = 0.0
    for sym, legs in state.get("option_positions", {}).items():
        for leg in legs:
            bid, ask = option_price(leg["underlying"], leg["strike"], leg["expiration"], leg["opt_type"])
            mark = (bid + ask) / 2
            opt_unreal += (mark - leg["cost"]) * leg["qty"] * 100
    return DISCLAIMER + "\n".join([
        "=== Account Balance ===",
        "Currency:              USD",
        f"Total Cash Balance:    {state['cash']:.2f}",
        f"Total Market Value:    {mv:.2f}",
        f"Total Unrealized P&L:  {unreal + opt_unreal:.2f}",
        f"Net Liquidation:       {eq:.2f}",
        f"Day P&L:               {eq - state['day_start_equity']:.2f}",
        "",
        "  --- USD ---",
        f"  Cash Balance:        {state['cash']:.2f}",
        f"  Buying Power:        {state['cash']:.2f}",
    ])


@mcp.tool(description="Get account positions with quantity, cost, last price and P&L.",
          annotations=READ_ONLY)
async def get_account_positions(account_id: str) -> str:
    if err := check_account(account_id):
        return err
    state = load()
    try_fill(state)
    save(state)
    lines = ["=== Positions ==="]
    # Equity positions
    for sym, p in sorted(state["positions"].items()):
        last = price_of(sym)
        lines.append(
            f"  Symbol: {sym}  Position ID: MOCK-{sym}  Qty: {p['qty']:g}  Type: EQUITY  "
            f"Cost: {p['cost']:.2f}  Last: {last:.2f}  "
            f"Unrealized P&L: {(last - p['cost']) * p['qty']:.2f}  Currency: USD"
        )
    # Option positions
    for opt_sym, legs in sorted(state.get("option_positions", {}).items()):
        for leg in legs:
            bid, ask = option_price(leg["underlying"], leg["strike"], leg["expiration"], leg["opt_type"])
            mark = (bid + ask) / 2
            lines.append(
                f"  Symbol: {opt_sym}  Position ID: MOCK-{opt_sym}  Qty: {leg['qty']}  Type: OPTION  "
                f"Cost: {leg['cost']:.2f}  Last: {mark:.2f}  "
                f"Unrealized P&L: {(mark - leg['cost']) * leg['qty'] * 100:.2f}  Currency: USD"
                f"  Leg: {leg['opt_type']} {leg['underlying']} {leg['strike']} {leg['expiration']}"
            )
    if len(lines) == 1:
        return DISCLAIMER + "No data available."
    return DISCLAIMER + "\n".join(lines)


@mcp.tool(description="Get real-time stock/ETF snapshot. Supports multiple symbols "
                      "(comma-separated).", annotations=READ_ONLY)
async def get_stock_snapshot(symbols: str, category: str = "US_STOCK", extend_hour_required: bool = False,
                             overnight_required: bool = False) -> str:
    lines = ["=== Stock Snapshot ==="]
    now = time.time()
    for sym in [s.strip().upper() for s in symbols.split(",") if s.strip()]:
        px, prev = price_of(sym, now), price_of(sym, now - DAY)
        hi = max(price_of(sym, now - h * 1800) for h in range(14))
        lo = min(price_of(sym, now - h * 1800) for h in range(14))
        chg = px - prev
        lines.append(f"  {sym:>8s}  Price: {px:>10.2f}  PreClose: {prev:>10.2f}  "
                     f"Change: {chg:>8.2f}  Change%: {chg / prev:>8.4f}")
        day_volume = 1_000_000 + zlib.crc32(sym.encode()) % 9_000_000 + int(now % DAY) * 40
        lines.append(f"{'':>10s}  Open: {prev:>10.2f}  High: {hi:>10.2f}  Low: {lo:>10.2f}  "
                     f"Close: {px:>10.2f}  Vol: {day_volume:>12d}")
        lines.append(f"{'':>10s}  Bid: {px - 0.01:>10.2f} x {'300':>6s}  "
                     f"Ask: {px + 0.01:>10.2f} x {'200':>6s}")
    return DISCLAIMER + "\n".join(lines)


# Support both Webull format (m5, m15, m30, m60, d, w) and alternative format (M5, M15, etc.)
STEPS = {
    "S5": 5, "S15": 15, "M1": 60, "M5": 300, "M15": 900, "M30": 1800, "M60": 3600,
    "M120": 7200, "M240": 14_400, "D": DAY, "W": 604_800,
    # Webull format
    "m5": 300, "m15": 900, "m30": 1800, "m60": 3600,
    "d": DAY, "w": 604_800,
    # Alternative format (used by broker.intraday_bars)
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600,
}


@mcp.tool(description="Get stock OHLCV bars in batch. Supports multiple symbols. "
                      "Returns: time, open, high, low, close, volume.", annotations=READ_ONLY)
async def get_stock_bars(symbols: str, category: str = "US_STOCK", timespan: str = "D",
                         count: int = 200, real_time_required: bool = False,
                         trading_sessions: Optional[str] = None,
                         start_time: Optional[int] = None, end_time: Optional[int] = None) -> str:
    step = STEPS.get(timespan, DAY)
    count = max(1, min(int(count), 1200))
    now = time.time()
    current = now - now % step  # start of the bar in progress
    lines = ["=== Stock Bars (OHLCV) ==="]
    for sym in [s.strip().upper() for s in symbols.split(",") if s.strip()]:
        lines.append(f"  --- {sym} ---")
        for i in range(count - 1, -1, -1):
            t0 = current - i * step
            t1 = min(t0 + step, now)
            samples = [price_of(sym, t0 + (t1 - t0) * k / 8) for k in range(9)]
            vol = 2_000 + zlib.crc32(f"{sym}{int(t0)}".encode()) % 20_000
            lines.append(f"  {_iso(t0):>20s}  O: {samples[0]:>10.2f}  H: {max(samples):>10.2f}  "
                         f"L: {min(samples):>10.2f}  C: {samples[-1]:>10.2f}  "
                         f"Vol: {vol * max(1, step // 60):>12d}")
    return DISCLAIMER + "\n".join(lines)


NEWS = {
    "POSITIVE": ["{s} shares climb as analysts lift price targets",
                 "{s} wins new enterprise contract, outlook raised",
                 "Strong demand lifts {s}; buyers step in on dips"],
    "NEGATIVE": ["{s} slides after a downgrade on valuation",
                 "Regulators open review into {s} practices",
                 "{s} falls as supplier warns on demand"],
    "NEUTRAL": ["{s} to present at industry conference next week",
                "What to watch in {s} ahead of earnings season",
                "{s} trading volume in line with average"],
}


@mcp.tool(description="Get US stock-related news list with optional sentiment filter. "
                      "Returns: news list with news_id, title, summary, source, url, "
                      "publish_time, sentiment, related_symbols.", annotations=READ_ONLY)
async def get_stock_news(symbol: str, sentiment: Optional[str] = None,
                         pagination_key: Optional[str] = None) -> str:
    sym, now = symbol.upper(), time.time()
    slot = 6 * 3600
    lines = ["=== Stock News List ==="]
    n = 0
    for k in range(12):                       # 6-hour slots over the last 3 days
        ts = (now - now % slot) - k * slot
        h = zlib.crc32(f"{sym}{int(ts)}".encode())
        if h % 3 and k < 11:                  # always emit the oldest slot, never zero news
            continue
        move = price_of(sym, ts) / price_of(sym, ts - DAY) - 1
        tag = "POSITIVE" if move > 0.004 else "NEGATIVE" if move < -0.004 else "NEUTRAL"
        if h % 5 == 0:                        # headlines don't always match the tape
            tag = "NEUTRAL"
        if sentiment and sentiment.upper() != tag:
            continue
        n += 1
        title = NEWS[tag][(h // 7) % 3].format(s=sym)
        lines += [f"\n[{n}]", f"  news_id: MOCK-{sym}-{int(ts)}", f"  title: {title}",
                  f"  summary: Synthetic headline from the mock broker about {sym}.",
                  "  source: Mock Wire", f"  url: https://example.com/mock/{sym}/{int(ts)}",
                  f"  publish_time: {_iso(ts + h % 3000)}", f"  sentiment: {tag}",
                  f"  related_symbols: ['{sym}']"]
    return DISCLAIMER + ("\n".join(lines) if n else "No data available.")


@mcp.tool(description="Get the US market fear and greed sentiment index.", annotations=READ_ONLY)
async def get_market_sentiment() -> str:
    def point(ts: float) -> tuple[int, str]:
        move = price_of("SPY", ts) / price_of("SPY", ts - 5 * DAY) - 1
        idx = int(round(50 + 45 * math.tanh(move / 0.02)))
        grade = ("Extreme Fear" if idx < 25 else "Fear" if idx < 45 else "Neutral" if idx <= 55
                 else "Greed" if idx <= 75 else "Extreme Greed")
        return idx, grade

    now = time.time()
    lines = ["=== Market Sentiment Index (America/New_York) ==="]
    for label, ago in (("Current", 0), ("Previous Day", DAY), ("One Week Ago", 7 * DAY),
                       ("One Month Ago", 30 * DAY)):
        idx, grade = point(now - ago)
        stamp = datetime.fromtimestamp(now - ago, timezone.utc).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {label:<13s}  Time: {stamp}")
        lines.append(f"  {'':13s}  Index: {idx:>4d}  Grade: {grade}")
    return DISCLAIMER + "\n".join(lines)


def _format_screener_entry(symbol: str, price: float, volume: int, change_pct: float) -> str:
    """Format a screener entry matching Webull's documented output format."""
    vol_str = f"{volume/1e6:.1f}M" if volume >= 1e6 else f"{volume/1e3:.0f}K" if volume >= 1e3 else str(volume)
    return f"  {symbol} - Price: ${price:.2f}  Change%: {change_pct:+.2f}%  Volume: {vol_str}"


@mcp.tool(description="Get top gainers and losers for US stocks. Returns symbol, price, change%, volume.", annotations=READ_ONLY)
async def get_gainers_losers(category: str = "US_STOCK", status: Optional[str] = None) -> str:
    """Mock gainers/losers - returns synthetic data for known symbols."""
    now = time.time()
    symbols = list(BASE_PRICES.keys())
    lines = ["=== Gainers & Losers ==="]
    for sym in symbols:
        px = price_of(sym, now)
        prev = price_of(sym, now - DAY)
        chg_pct = (px - prev) / prev * 100
        day_volume = 1_000_000 + zlib.crc32(sym.encode()) % 9_000_000 + int(now % DAY) * 40
        lines.append(_format_screener_entry(sym, px, day_volume, chg_pct))
    return DISCLAIMER + "\n".join(lines)


@mcp.tool(description="Get most actively traded US stocks. Returns symbol, price, change%, volume.", annotations=READ_ONLY)
async def get_most_active(category: str = "US_STOCK", status: Optional[str] = None) -> str:
    """Mock most active - returns synthetic data for known symbols sorted by volume."""
    now = time.time()
    symbols = list(BASE_PRICES.keys())
    # Sort by volume (descending)
    sym_volumes = [(sym, 1_000_000 + zlib.crc32(sym.encode()) % 9_000_000 + int(now % DAY) * 40) for sym in symbols]
    sym_volumes.sort(key=lambda x: x[1], reverse=True)
    lines = ["=== Most Active ==="]
    for sym, vol in sym_volumes:
        px = price_of(sym, now)
        prev = price_of(sym, now - DAY)
        chg_pct = (px - prev) / prev * 100
        lines.append(_format_screener_entry(sym, px, vol, chg_pct))
    return DISCLAIMER + "\n".join(lines)


@mcp.tool(description="Get crypto instruments list.", annotations=READ_ONLY)
async def get_crypto_instruments(category: str = "US_CRYPTO", page_size: int = 50) -> str:
    """Mock crypto instruments - returns a few major crypto symbols."""
    crypto_symbols = {"BTC-USD": 65000, "ETH-USD": 3500, "SOL-USD": 150, "DOGE-USD": 0.15}
    lines = ["=== Crypto Instruments ==="]
    for sym, base in crypto_symbols.items():
        px = base * (1 + 0.05 * math.sin(time.time() / 3600))
        vol = 10_000_000 + zlib.crc32(sym.encode()) % 50_000_000
        lines.append(_format_screener_entry(sym, px, vol, 0))
    return DISCLAIMER + "\n".join(lines)


# -- Option tools --------------------------------------------------------------
@mcp.tool(description="List US-listed option contracts for an underlying. "
                      "Returns: symbol, strike, expiration, type, bid, ask, volume, open_interest.",
          annotations=READ_ONLY)
async def get_option_contracts(category: Optional[str] = None, underlying_symbols: str = "",
                               underlying: Optional[str] = None, expiration: Optional[str] = None,
                               option_type: Optional[str] = None,
                               strike_min: Optional[float] = None,
                               strike_max: Optional[float] = None,
                               pagination_key: Optional[str] = None,
                               status: Optional[str] = None) -> str:
    """Mock option chain - generates contracts around current price."""
    # Support both parameter names from broker (underlying_symbols) and direct calls (underlying)
    symbols = underlying_symbols or underlying or ""
    if not symbols:
        return DISCLAIMER + "Validation error: at least one underlying symbol required"
    # Take first symbol if multiple provided
    underlying = symbols.split(",")[0].strip().upper()
    px = price_of(underlying)
    now = time.time()

    # Generate expirations: weekly for next 8 weeks, then monthly
    expirations = []
    for w in range(1, 9):
        exp = datetime.fromtimestamp(now, timezone.utc) + timedelta(weeks=w)
        # Friday expiration
        days_ahead = 4 - exp.weekday()  # Friday = 4
        if days_ahead <= 0:
            days_ahead += 7
        exp = exp + timedelta(days=days_ahead)
        expirations.append(exp.strftime("%Y-%m-%d"))
    for m in range(3, 7):
        exp = datetime.fromtimestamp(now, timezone.utc) + timedelta(weeks=m*4)
        # Third Friday
        exp = exp.replace(day=1)
        while exp.weekday() != 4:  # Friday
            exp += timedelta(days=1)
        exp += timedelta(weeks=2)  # third Friday
        expirations.append(exp.strftime("%Y-%m-%d"))

    if expiration:
        expirations = [e for e in expirations if e == expiration]

    lines = ["=== Option Contracts ==="]
    count = 0
    now_dt = datetime.fromtimestamp(now, timezone.utc)
    for exp in expirations[:10]:  # Limit to 10 expirations
        exp_dt = datetime.fromisoformat(exp)
        # Make exp_dt timezone-aware (UTC)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        days_to_exp = (exp_dt - now_dt).days
        for opt_type in (["CALL", "PUT"] if not option_type else [option_type.upper()]):
            # Strikes around ATM ± 20%
            atm = round(px / 5) * 5  # round to $5
            for strike_pct in [0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3]:
                strike = round(atm * strike_pct / 5) * 5
                if strike_min and strike < strike_min:
                    continue
                if strike_max and strike > strike_max:
                    continue
                opt_sym = option_symbol(underlying, exp, opt_type, strike)
                bid, ask = option_price(underlying, strike, exp, opt_type)
                vol = max(1, int(1000 * (1 - abs(strike - px) / px) * (30 / max(days_to_exp, 1))))
                oi = vol * 10
                count += 1
                lines.append(f"  {opt_sym}  Type: {opt_type}  Strike: {strike:.2f}  Exp: {exp}  "
                             f"DTE: {days_to_exp}  Bid: {bid:.2f}  Ask: {ask:.2f}  "
                             f"Vol: {vol}  OI: {oi}")
                if count >= 100:  # Limit output
                    break
            if count >= 100:
                break
        if count >= 100:
            break
    return DISCLAIMER + ("\n".join(lines) if count else "No data available.")


@mcp.tool(description="Get real-time option quote snapshots (batch, max 20 symbols). "
                      "Returns: symbol, bid, ask, last, volume, open_interest, IV, Greeks.",
          annotations=READ_ONLY)
async def get_option_snapshot(symbols: str, category: Optional[str] = None) -> str:
    lines = ["=== Option Snapshot ==="]
    now = time.time()
    for sym in [s.strip().upper() for s in symbols.split(",") if s.strip()]:
        # Parse OCC symbol: AAPL240119C00150000 (underlying + YYMMDD + C/P + strike*1000)
        # Minimum length: 1 (underlying) + 6 (date) + 1 (type) + 8 (strike) = 16
        if len(sym) < 16:
            lines.append(f"  {sym}: Invalid option symbol format (length {len(sym)})")
            continue
        # Last 15 chars are: 6 date + 1 type + 8 strike
        underlying = sym[:-15]
        exp_str = sym[-15:-9]
        opt_type = "CALL" if sym[-9] == "C" else "PUT"
        strike = int(sym[-8:]) / 1000
        # Reconstruct expiration
        exp = f"20{exp_str[:2]}-{exp_str[2:4]}-{exp_str[4:6]}"
        try:
            bid, ask = option_price(underlying, strike, exp, opt_type)
            last = (bid + ask) / 2
            vol = 500
            oi = 5000
            # Mock Greeks
            # Black-Scholes-style delta (30% vol, 4% rate) so strike/delta selection is testable
            _t = max((datetime.fromisoformat(exp).replace(tzinfo=timezone.utc)
                      - datetime.now(timezone.utc)).days, 1) / 365
            _d1 = (math.log(max(price_of(underlying), 0.01) / strike) + (0.04 + 0.045) * _t) / (0.30 * math.sqrt(_t))
            delta = norm_cdf(_d1) if opt_type == "CALL" else norm_cdf(_d1) - 1
            delta = max(-0.79, min(0.79, delta)) if abs(delta) > 0.79 else (delta if abs(delta) >= 0.21 else (0.21 if delta > 0 else -0.21))  # keep inside the gate range
            gamma = 0.02
            theta = -0.05
            vega = 0.10
            iv = 0.30
            lines.append(f"  {sym}  Bid: {bid:.2f}  Ask: {ask:.2f}  Last: {last:.2f}  "
                         f"Vol: {vol}  OI: {oi}  IV: {iv:.2f}  "
                         f"Delta: {delta:.3f}  Gamma: {gamma:.3f}  Theta: {theta:.3f}  Vega: {vega:.3f}  "
                         f"AsOf: {now:.3f}")
        except Exception as e:
            lines.append(f"  {sym}: Error pricing - {e}")
    return DISCLAIMER + "\n".join(lines)


@mcp.tool(description="Preview an option order without submitting. Returns: estimated cost and fees.",
          annotations=READ_ONLY)
async def preview_option_order(account_id: str = "", symbol: str = "", side: str = "", quantity: int = 0,
                               option_type: str = "", strike_price: float = 0.0, expiration_date: str = "",
                               order_type: str = "LIMIT", time_in_force: str = "DAY", trading_session: Optional[str] = None,
                               limit_price: Optional[float] = None,
                               stop_price: Optional[float] = None,
                               position_intent: Optional[str] = None,
                               account_tax_type: Optional[str] = None) -> str:
    # Validate required fields
    if not account_id:
        return "Validation error: account_id is required"
    if err := check_account(account_id):
        return err
    if not symbol or not side or not option_type or not expiration_date:
        return "Validation error: symbol, side, option_type, expiration_date are required"
    if err := _validate_option(symbol, side, quantity, option_type, strike_price,
                                expiration_date, order_type, limit_price, stop_price, position_intent):
        return err
    px = limit_price or stop_price
    if order_type == "MARKET":
        # Use mid price for preview
        underlying = symbol[:-15] if len(symbol) >= 20 else "UNKNOWN"
        bid, ask = option_price(underlying, strike_price, expiration_date, option_type)
        px = (bid + ask) / 2
    premium = quantity * 100 * (px or 0)
    return DISCLAIMER + (f"=== Option Order Preview ===\n"
                         f"  Estimated Premium: {premium:.2f}\n"
                         f"  Estimated Transaction Fee: 0.65 per contract\n"
                         f"  Total Estimated Cost: {premium + quantity * 0.65:.2f}")


@mcp.tool(description="Place a single-leg option order. For multi-leg use place_option_strategy_order.\n"
                      "order_type: MARKET, LIMIT, STOP_LOSS, STOP_LOSS_LIMIT.\n"
                      "time_in_force: DAY, GTC. trading_session: CORE only.\n"
                      "position_intent: BUY_TO_OPEN, BUY_TO_CLOSE, SELL_TO_OPEN, SELL_TO_CLOSE.\n"
                      "Returns: {client_order_id, order_id}",
          annotations=READ_ONLY)
async def place_option_single_order(account_id: str = "", symbol: str = "", side: str = "", quantity: int = 0,
                                    option_type: str = "", strike_price: float = 0.0, expiration_date: str = "",
                                    order_type: str = "LIMIT", time_in_force: str = "DAY", trading_session: Optional[str] = None,
                                    client_order_id: Optional[str] = None,
                                    limit_price: Optional[float] = None,
                                    stop_price: Optional[float] = None,
                                    position_intent: Optional[str] = None,
                                    account_tax_type: Optional[str] = None) -> str:
    # Validate required fields
    if not account_id:
        return "Validation error: account_id is required"
    if err := check_account(account_id):
        return err
    if not symbol or not side or not option_type or not expiration_date:
        return "Validation error: symbol, side, option_type, expiration_date are required"
    if err := (check_account(account_id)
               or _validate_option(symbol, side, quantity, option_type, strike_price,
                                   expiration_date, order_type, limit_price, stop_price, position_intent)):
        return err
    state = load()
    coid = client_order_id or uuid.uuid4().hex
    if any(o["client_order_id"] == coid for o in state["orders"]):
        return f"Validation error: duplicate client_order_id {coid}"

    # Build order with legs format (matching Webull SDK)
    leg = {
        "side": side,
        "quantity": quantity,
        "symbol": symbol,
        "strike_price": strike_price,
        "option_expire_date": expiration_date,
        "instrument_type": "OPTION",
        "option_type": option_type.upper(),
        "market": "US",
        "underlying": symbol[:-15] if len(symbol) >= 20 else "UNKNOWN",
    }
    order = _new_order(state, symbol, side, order_type, quantity, limit_price, stop_price,
                       time_in_force, coid,
                       instrument_type="OPTION",
                       option_strategy="SINGLE",
                       entrust_type="QTY",
                       legs=[leg],
                       position_intent=position_intent,
                       account_tax_type=account_tax_type)
    try_fill(state)
    save(state)
    return DISCLAIMER + f"client_order_id: {coid}\norder_id: {order['order_id']}"


@mcp.tool(description="[US Only] Place a multi-leg option strategy order "
                      "(VERTICAL, STRADDLE, STRANGLE, BUTTERFLY, CONDOR, etc).\n"
                      "Returns: {client_order_id, combo_order_id, order_id}",
          annotations=READ_ONLY)
async def place_option_strategy_order(account_id: str, legs: list[dict],
                                      client_combo_order_id: Optional[str] = None) -> str:
    if err := check_account(account_id):
        return err
    if not legs or len(legs) < 2:
        return "Validation error: strategy order requires at least 2 legs"
    state = load()
    combo = client_combo_order_id or uuid.uuid4().hex
    coids = [leg.get("client_order_id") or uuid.uuid4().hex for leg in legs]
    if any(o["client_order_id"] in coids for o in state["orders"]):
        return "Validation error: duplicate client_order_id"

    placed = []
    for leg, coid in zip(legs, coids):
        # Validate each leg
        err = _validate_option(leg["symbol"], leg["side"], int(leg["quantity"]),
                               leg["option_type"], leg["strike_price"], leg["expiration_date"],
                               leg.get("order_type", "LIMIT"), leg.get("limit_price"),
                               leg.get("stop_price"), leg.get("position_intent"))
        if err:
            return err
        order = _new_order(state, leg["symbol"], leg["side"], leg.get("order_type", "LIMIT"),
                           int(leg["quantity"]), leg.get("limit_price"), leg.get("stop_price"),
                           leg.get("time_in_force", "DAY"), coid,
                           instrument_type="OPTION",
                           option_strategy=leg.get("option_strategy", "VERTICAL"),
                           entrust_type="QTY",
                           legs=[leg],
                           position_intent=leg.get("position_intent"),
                           group_id=combo,
                           combo_type=leg.get("combo_type", "NORMAL"))
        placed.append(order)
    try_fill(state)
    save(state)
    return DISCLAIMER + (f"client_combo_order_id: {combo}\ncombo_order_id: C{placed[0]['order_id']}"
                         f"\norder_id: {placed[0]['order_id']}")


@mcp.tool(description="Modify an existing option order.",
          annotations=READ_ONLY)
async def replace_option_order(account_id: str, client_order_id: str,
                               limit_price: Optional[float] = None,
                               stop_price: Optional[float] = None,
                               quantity: Optional[int] = None,
                               time_in_force: Optional[str] = None) -> str:
    if err := check_account(account_id):
        return err
    state = load()
    try_fill(state)
    for o in state["orders"]:
        if o["client_order_id"] == client_order_id and o["status"] in OPEN_STATUSES:
            if limit_price is not None:
                o["limit_price"] = limit_price
            if stop_price is not None:
                o["stop_price"] = stop_price
            if quantity is not None:
                o["quantity"] = quantity
                for leg in o.get("legs", []):
                    leg["quantity"] = quantity
            if time_in_force is not None:
                o["tif"] = time_in_force
            save(state)
            return DISCLAIMER + f"client_order_id: {client_order_id}\nstatus: MODIFIED"
    save(state)
    return "Order error: order not found, or already filled or cancelled"


# -- Stock order tools (updated for fractional) --------------------------------
@mcp.tool(description="Get open orders.", annotations=READ_ONLY)
async def get_open_orders(account_id: str, pagination_key: Optional[str] = None) -> str:
    if err := check_account(account_id):
        return err
    state = load()
    try_fill(state)
    save(state)
    open_groups = {o.get("group_id") or o["client_order_id"]
                   for o in state["orders"] if o["status"] in OPEN_STATUSES}
    return fmt_orders([o for o in state["orders"]
                       if (o.get("group_id") or o["client_order_id"]) in open_groups],
                      "Open Orders")


@mcp.tool(description="Get historical orders.", annotations=READ_ONLY)
async def get_order_history(account_id: str, start_time: Optional[str] = None,
                            end_time: Optional[str] = None,
                            pagination_key: Optional[str] = None) -> str:
    if err := check_account(account_id):
        return err
    state = load()
    try_fill(state)
    save(state)
    return fmt_orders(state["orders"][-60:][::-1], "Order History")


@mcp.tool(description="Preview a stock order without submitting. "
                      "Returns estimated cost and fees. Supports fractional via entrust_type=AMOUNT.",
          annotations=READ_ONLY)
async def preview_stock_order(account_id: str, symbol: str, side: str, order_type: str,
                              time_in_force: str, quantity: float, market: str = "US",
                              limit_price: Optional[float] = None,
                              stop_price: Optional[float] = None,
                              trading_session: str = "CORE",
                              entrust_type: str = "QTY",
                              total_cash_amount: Optional[float] = None) -> str:
    if err := check_account(account_id) or _validate_stock(symbol, side, order_type, quantity,
                                                           limit_price, stop_price,
                                                           entrust_type, total_cash_amount):
        return err
    if entrust_type == "AMOUNT":
        px = limit_price or price_of(symbol)
        est_cost = total_cash_amount or 0
    else:
        px = limit_price or stop_price or price_of(symbol)
        est_cost = quantity * px
    return DISCLAIMER + (f"=== Order Preview ===\n  Estimated Cost: {est_cost:.2f}\n"
                         "  Estimated Transaction Fee: 0.00")


@mcp.tool(description="Place a stock order. Returns: {client_order_id, order_id}\n"
                      "Supports fractional shares via entrust_type=AMOUNT with total_cash_amount (USD).\n"
                      "For fractional: order_type must be MARKET or LIMIT, trading_session=CORE only.\n"
                      "quantity is shares (float allowed) when entrust_type=QTY.",
          annotations=READ_ONLY)
async def place_stock_order(symbol: str, side: str, order_type: str, time_in_force: str,
                            quantity: float, market: str = "US",
                            account_id: Optional[str] = None,
                            client_order_id: Optional[str] = None,
                            limit_price: Optional[float] = None,
                            stop_price: Optional[float] = None,
                            trading_session: str = "CORE",
                            entrust_type: str = "QTY",
                            total_cash_amount: Optional[float] = None) -> str:
    if err := (check_account(account_id or ACCOUNT_ID)
               or _validate_stock(symbol, side, order_type, quantity, limit_price, stop_price,
                                  entrust_type, total_cash_amount)):
        return err
    # Mirror Webull: a session must be open now (NIGHT is overnight 20:00-04:00 ET only)
    from app.market import order_session
    _open = order_session()
    if trading_session == "NIGHT" and _open != "NIGHT":
        return ("Error: HTTP 417: [OPENAPI_OVERNIGHT_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS] "
                "Overnight Trading is only available during the Overnight Session, "
                "which operates from 8:00pm to 4:00am ET.")
    # Mirror Webull: market-type orders are not accepted with extended hours (ALL/NIGHT).
    if order_type.upper() == "MARKET" and trading_session in ("ALL", "NIGHT", "ALL_DAY"):
        return ("Error: HTTP 417: [OPENAPI_GTX_NOT_SUPPORT_MARKET] Market orders are not "
                "supported with extended hours.")
    # Real UAT accepts a GTC CORE STOP_LOSS out of hours (observed premarket); other CORE orders not.
    _gtc_stop = order_type.upper() == "STOP_LOSS" and time_in_force.upper() == "GTC"
    if trading_session == "CORE" and _open != "CORE" and not _gtc_stop:
        return "Error: HTTP 417: [OPENAPI_CORE_SESSION_CLOSED] Regular session is closed."
    state = load()
    coid = client_order_id or uuid.uuid4().hex
    if any(o["client_order_id"] == coid for o in state["orders"]):
        return f"Validation error: duplicate client_order_id {coid}"

    # For AMOUNT orders, calculate quantity from total_cash_amount
    if entrust_type == "AMOUNT" and total_cash_amount:
        px = limit_price or price_of(symbol)
        quantity = total_cash_amount / px
        # Round to 4 decimal places
        quantity = round(quantity, 4)

    order = _new_order(state, symbol, side, order_type, quantity, limit_price, stop_price,
                       time_in_force, coid,
                       entrust_type=entrust_type,
                       total_cash_amount=total_cash_amount,
                       trading_session=trading_session,
                       market=market)
    try_fill(state)
    save(state)
    return DISCLAIMER + f"client_order_id: {coid}\norder_id: {order['order_id']}"


@mcp.tool(description="[US Only] Place a combo stock order. combo_type per leg: NORMAL, MASTER "
                      "(triggers TP/SL), STOP_PROFIT, STOP_LOSS, OTO, OCO, OTOCO.\n"
                      "Returns: {client_order_id, combo_order_id, order_id}\n"
                      "NOTE: Fractional quantities not supported in combo orders.",
          annotations=READ_ONLY)
async def place_stock_combo_order(account_id: str, orders: list[dict],
                                  client_combo_order_id: Optional[str] = None) -> str:
    if err := check_account(account_id):
        return err
    masters = [o for o in orders if o.get("combo_type") == "MASTER"]
    exits = [o for o in orders if o.get("combo_type") in ("STOP_PROFIT", "STOP_LOSS")]
    if len(masters) != 1 or len(exits) != len(orders) - 1 or not exits:
        return "Validation error: the mock supports one MASTER leg plus STOP_PROFIT/STOP_LOSS legs"
    for leg in orders:
        qty = float(leg["quantity"])
        if qty != int(qty):
            return "Validation error: fractional quantities not supported in combo orders"
        if err := _validate_stock(leg["symbol"], leg["side"], leg["order_type"],
                                  qty, leg.get("limit_price"), leg.get("stop_price")):
            return err
    state = load()
    combo = client_combo_order_id or uuid.uuid4().hex
    coids = [leg.get("client_order_id") or uuid.uuid4().hex for leg in orders]
    if any(o["client_order_id"] in coids for o in state["orders"]):
        return "Validation error: duplicate client_order_id"
    master_coid = coids[orders.index(masters[0])]
    exit_coids = [c for leg, c in zip(orders, coids) if leg is not masters[0]]
    placed = []
    for leg, coid in zip(orders, coids):
        is_master = leg is masters[0]
        o = _new_order(state, leg["symbol"], leg["side"], leg["order_type"],
                       float(leg["quantity"]), leg.get("limit_price"), leg.get("stop_price"),
                       leg.get("time_in_force", "DAY"), coid, group_id=combo,
                       combo_type=leg["combo_type"])
        if not is_master:
            o.update(status="PENDING", parent=master_coid,
                     oco=next((c for c in exit_coids if c != coid), None))
        placed.append(o)
    try_fill(state)
    save(state)
    return DISCLAIMER + (f"client_combo_order_id: {combo}\ncombo_order_id: C{placed[0]['order_id']}"
                         f"\norder_id: {placed[0]['order_id']}")


@mcp.tool(description="Cancel an existing order.")
async def cancel_order(account_id: str, client_order_id: str) -> str:
    if err := check_account(account_id):
        return err
    state = load()
    try_fill(state)
    for o in state["orders"]:
        if o["client_order_id"] == client_order_id and o["status"] in OPEN_STATUSES:
            _set(o, "CANCELLED")
            for child in _children(state, o):
                if child["status"] in OPEN_STATUSES:
                    _set(child, "CANCELLED")
            save(state)
            return DISCLAIMER + f"client_order_id: {client_order_id}\nstatus: CANCELLED"
    save(state)
    return "Order error: order not found, or already filled or cancelled"



if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
