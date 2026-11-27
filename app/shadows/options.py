"""One-leg option shadows: virtual option trades priced from real Webull option quotes (docs/SHADOWS.md).

Hard rule (same as manager.py): only broker READ methods are called here: ``option_contracts``,
``parsed_option_snapshots`` and, through the manager, ``quote`` / ``last_price``. Never an order method.

Cycle: open one leg (registry.OptionSpec) when there is none, the gate allows it and the CORE session is in
the rebalance window; close it early only when the gate turns off (``close_on_gate_off``); otherwise hold to
expiry, where it is cash-settled at intrinsic value from the underlying price (the virtual equivalent of
assignment plus selling the shares at once, the same simplification as the put-write backtest). No new
leg on the day a cycle ended. Fills: sell at mid - 25% of the spread (never below the bid), buy at
mid + 25% of the spread (never above the ask), $0.65 per contract. Equity = cash + qty x 100 x mid.
"""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from statistics import NormalDist

from ..engine.util import parse_occ
from ..market import is_trading_day, now_et, session_close
from ..sleeves.strategies.putwrite import round_tick, sell_open_limit

log = logging.getLogger("app.shadows")
MULTIPLIER = 100
COMM = 0.65                      # $ per contract per side (same as the options research)
BUY_SPREAD_FRAC = 0.25
NEAR_STRIKES = 12                # snapshot requests are limited to 20 contracts
SETTLE_BEFORE_CLOSE_MIN = 15
NO_OPEN_BEFORE_CLOSE_MIN = 20
WARN_EVERY_SECONDS = 3600.0
_OCC_TOKEN = re.compile(r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b")


def _f(v) -> float | None:
    try:
        x = None if v is None else float(v)
    except (TypeError, ValueError):
        return None
    return x if x is not None and math.isfinite(x) else None


def _warn(mgr, sid: str, key: str, msg: str) -> None:
    k = f"opt:{sid}:{key}"
    if time.monotonic() >= mgr._price_warned.get(k, 0.0):
        mgr._price_warned[k] = time.monotonic() + WARN_EVERY_SECONDS
        mgr.db.log("warn", msg)


def _close_dt(day: date) -> datetime:
    from ..market import ET
    return datetime.combine(day, session_close(day) or dtime(16, 0)).replace(tzinfo=ET)


def is_expired(exp: date, now: datetime) -> bool:
    """Past expiry, or expiry day within the last 15 minutes of the session."""
    today = now.date()
    if exp < today:
        return True
    return exp == today and now >= _close_dt(exp) - timedelta(minutes=SETTLE_BEFORE_CLOSE_MIN)


def near_close(now: datetime) -> bool:
    return (is_trading_day(now.date())
            and now >= _close_dt(now.date()) - timedelta(minutes=NO_OPEN_BEFORE_CLOSE_MIN))


def intrinsic(right: str, strike: float, spot: float) -> float:
    return max(strike - spot, 0.0) if right == "PUT" else max(spot - strike, 0.0)


def est_strike(spot: float, right: str, delta: float, vol: float, dte: int) -> float:
    """Strike whose |delta| ~= delta under a flat vol (only to choose which strikes to quote)."""
    d1 = NormalDist().inv_cdf(delta if right == "CALL" else 1 - delta)
    return spot * math.exp(-d1 * vol * math.sqrt(max(dte, 1) / 365))


def candidates(text: str, spec, today: date) -> list[dict]:
    """OCC contracts of ``spec.underlying`` / ``spec.right`` with standard strikes, dte_min..dte_max."""
    rows = []
    for tok in dict.fromkeys(_OCC_TOKEN.findall(str(text or "").upper())):
        occ = parse_occ(tok)
        if not occ or occ["underlying"] != spec.underlying or occ["option_type"] != spec.right:
            continue
        if abs(occ["strike"] * 2 - round(occ["strike"] * 2)) > 1e-6:      # adjusted contracts: odd strikes
            continue
        exp = date.fromisoformat(occ["expiration"])
        if spec.dte_min <= (exp - today).days <= spec.dte_max:
            rows.append({**occ, "symbol": tok, "expiration": exp})
    return rows


def pick(chain: list[dict], spec, est: float) -> dict | None:
    """Two-sided quote nearest the target |delta| (strike nearest the estimate when deltas are missing)."""
    ok = [r for r in chain if (_f(r.get("bid")) or 0) > 0 and (_f(r.get("ask")) or 0) >= (_f(r.get("bid")) or 0)]
    if not ok:
        return None
    with_delta = [r for r in ok if _f(r.get("delta")) is not None]
    if with_delta:
        return min(with_delta, key=lambda r: (abs(abs(_f(r["delta"])) - spec.delta), r["strike"]))
    return min(ok, key=lambda r: abs(r["strike"] - est))


def buy_limit(bid: float, ask: float, tick=None) -> float:
    px = round_tick((bid + ask) / 2 + BUY_SPREAD_FRAC * (ask - bid), "nearest", tick)
    return min(px, round_tick(ask, "down", tick)) if ask > 0 else px


def sell_limit(bid: float, ask: float, tick=None) -> float:
    return sell_open_limit(bid, ask, tick)


def mark_of(q: dict | None) -> float | None:
    q = q or {}
    bid, ask, last = _f(q.get("bid")), _f(q.get("ask")), _f(q.get("last"))
    if bid and ask and ask >= bid:
        return (bid + ask) / 2
    return last or ask or bid


# ---------------------------------------------------------------------------------------------- steps
async def step(mgr, d, window: bool) -> None:
    spec, sid = d.option, d.id
    now = now_et()
    today = now.date()
    holds = mgr.db.shadow_holdings(sid)
    leg = holds[0] if holds else None
    if leg is not None:
        occ = parse_occ(leg["symbol"])
        if not occ:      # cannot happen through this module; drop a corrupt row rather than value it forever
            mgr.db.set_shadow_holding(sid, leg["symbol"], 0, None)
            return
        if is_expired(date.fromisoformat(occ["expiration"]), now):
            await _settle(mgr, d, leg, occ)
        elif window and spec.close_on_gate_off:
            g = await mgr.gate_value(spec.gate)
            if g is not None and g < 0.5:
                await _close(mgr, d, leg, occ, f"trend gate off ({g:.2f})")
    elif window and not near_close(now):
        from .manager import _meta
        if _meta(mgr.db.shadow_state(sid)).get("cycle_end_day") != today.isoformat():
            g = await mgr.gate_value(spec.gate)
            if g is None:
                _warn(mgr, sid, "gate", f"Shadow {d.name}: no trend data for {spec.gate[0]}; not opening, retrying")
            elif g >= 0.5:
                await _open(mgr, d, g)
    await _value(mgr, d)


def _end_cycle(mgr, sid: str) -> None:
    from .manager import _meta
    meta = _meta(mgr.db.shadow_state(sid))
    meta["cycle_end_day"] = now_et().date().isoformat()
    mgr.db.upsert_shadow_state(sid, meta=meta)


async def _settle(mgr, d, leg: dict, occ: dict) -> None:
    sid = d.id
    spot = await mgr._price(sid, occ["underlying"])
    if not spot:
        _warn(mgr, sid, "settle", f"Shadow {d.name}: no {occ['underlying']} price to settle {leg['symbol']}; retrying")
        return
    qty = float(leg["qty"])
    val = intrinsic(occ["option_type"], float(occ["strike"]), float(spot))
    with mgr.db.transaction():
        mgr.db.upsert_shadow_state(sid, cash=round(mgr._cash(sid) + qty * MULTIPLIER * val, 2))
        mgr.db.set_shadow_holding(sid, leg["symbol"], 0, None)
        # recorded as the closing side (BUY closes a short) so the Results FIFO matches the round trip
        mgr.db.add_shadow_trade(sid, leg["symbol"], "BUY" if qty < 0 else "SELL", abs(qty), round(val, 4),
                                f"expired {occ['expiration']}: {'in the money' if val > 0 else 'worthless'}, "
                                f"cash-settled at intrinsic ({occ['underlying']} {spot:.2f})")
    _end_cycle(mgr, sid)
    mgr._px.setdefault(sid, {"at": -1e9, "prices": {}})["prices"].pop(leg["symbol"], None)
    mgr.db.log("info", f"Shadow {d.name}: {leg['symbol']} expired (virtual), settled at {val:.2f}")


async def _close(mgr, d, leg: dict, occ: dict, why: str) -> None:
    sid, sym = d.id, leg["symbol"]
    try:
        q = (await mgr.broker.parsed_option_snapshots([sym])).get(sym) or {}
    except Exception:  # noqa: BLE001 - any read failure: retry next tick
        q = {}
    bid, ask = _f(q.get("bid")), _f(q.get("ask"))
    qty = float(leg["qty"])
    n = abs(qty)
    if qty < 0:          # short: buy to close
        px = buy_limit(bid, ask, q.get("tick")) if bid and ask else ask
    else:
        px = sell_limit(bid, ask, q.get("tick")) if bid and ask else bid
    if not px:
        _warn(mgr, sid, "close", f"Shadow {d.name}: no quote to close {sym}; retrying")
        return
    sign = 1.0 if qty < 0 else -1.0            # cash out for a buy-to-close, in for a sell-to-close
    with mgr.db.transaction():
        mgr.db.upsert_shadow_state(sid, cash=round(mgr._cash(sid) - sign * px * MULTIPLIER * n - COMM * n, 2))
        mgr.db.set_shadow_holding(sid, sym, 0, None)
        mgr.db.add_shadow_trade(sid, sym, "BUY" if qty < 0 else "SELL", n, px, f"close: {why}")
    mgr._px.setdefault(sid, {"at": -1e9, "prices": {}})["prices"].pop(sym, None)
    mgr.db.log("info", f"Shadow {d.name}: closed {n:g} x {sym} @ {px:.2f} (virtual; {why})")


async def _open(mgr, d, gate: float) -> None:
    spec, sid = d.option, d.id
    today = now_et().date()
    spot = await mgr._price(sid, spec.underlying)
    if not spot:
        _warn(mgr, sid, "spot", f"Shadow {d.name}: no {spec.underlying} price; not opening, retrying")
        return
    try:
        rows = candidates(await mgr.broker.option_contracts([spec.underlying]), spec, today)
    except Exception as e:  # noqa: BLE001
        _warn(mgr, sid, "chain", f"Shadow {d.name}: {spec.underlying} option chain unavailable ({e}); retrying")
        return
    if not rows:
        _warn(mgr, sid, "chain", f"Shadow {d.name}: no {spec.underlying} {spec.right} expiring in "
                                 f"{spec.dte_min}-{spec.dte_max} days; retrying")
        return
    target = min({r["expiration"] for r in rows}, key=lambda e: (abs((e - today).days - spec.dte), e))
    dte = (target - today).days
    est = est_strike(float(spot), spec.right, spec.delta, spec.est_vol, dte)
    near = sorted((r for r in rows if r["expiration"] == target), key=lambda r: abs(r["strike"] - est))[:NEAR_STRIKES]
    try:
        snaps = await mgr.broker.parsed_option_snapshots([r["symbol"] for r in near])
    except Exception as e:  # noqa: BLE001
        _warn(mgr, sid, "quotes", f"Shadow {d.name}: option quotes unavailable ({e}); retrying")
        return
    chain = [{**r, **{k: (snaps.get(r["symbol"]) or {}).get(k) for k in ("bid", "ask", "delta", "tick")}} for r in near]
    best = pick(chain, spec, est)
    if not best:
        _warn(mgr, sid, "quotes", f"Shadow {d.name}: no two-sided {spec.underlying} {target} quote; retrying")
        return
    bid, ask = float(best["bid"]), float(best["ask"])
    cash = mgr._cash(sid)
    if spec.side == "short":
        px = sell_limit(bid, ask, best.get("tick"))
        n = int(math.floor(cash / (float(best["strike"]) * MULTIPLIER)))
    else:
        px = buy_limit(bid, ask, best.get("tick"))
        eq, _ = mgr._equity(sid)
        n = int(math.floor(eq * spec.budget_pct / (px * MULTIPLIER + COMM))) if px > 0 else 0
    if n < 1:
        _warn(mgr, sid, "size", f"Shadow {d.name}: cash ${cash:,.0f} does not cover one {best['symbol']}")
        return
    sym = best["symbol"]
    sign = 1.0 if spec.side == "short" else -1.0
    with mgr.db.transaction():
        mgr.db.upsert_shadow_state(sid, cash=round(cash + sign * px * MULTIPLIER * n - COMM * n, 2))
        mgr.db.set_shadow_holding(sid, sym, -n if spec.side == "short" else n, px)
        mgr.db.add_shadow_trade(sid, sym, "SELL" if spec.side == "short" else "BUY", n, px,
                                f"{'sell' if spec.side == 'short' else 'buy'} to open: delta "
                                f"{_f(best.get('delta'))}, {dte} DTE, {spec.underlying} {float(spot):.2f}, "
                                f"bid/ask {bid:.2f}/{ask:.2f}" + (f", gate {gate:.2f}" if spec.gate else ""))
    cache = mgr._px.setdefault(sid, {"at": -1e9, "prices": {}})
    cache["prices"][sym] = (bid + ask) / 2
    cache["at"] = time.monotonic()
    mgr.db.log("info", f"Shadow {d.name}: {'sold' if spec.side == 'short' else 'bought'} {n} x {sym} @ {px:.2f} "
                       f"(virtual, {dte} DTE)")


async def _value(mgr, d) -> None:
    """Mark the leg at the option mid at most every 5 minutes, then record today's equity."""
    from .manager import PRICE_REFRESH_SECONDS
    sid = d.id
    cache = mgr._px.setdefault(sid, {"at": -1e9, "prices": {}})
    holds = mgr.db.shadow_holdings(sid)
    if holds and time.monotonic() - cache["at"] >= PRICE_REFRESH_SECONDS:
        syms = [h["symbol"] for h in holds]
        try:
            snaps = await mgr.broker.parsed_option_snapshots(syms)
        except Exception:  # noqa: BLE001 - keep the previous mark
            snaps = {}
        for s in syms:
            m = mark_of(snaps.get(s))
            if m is not None:
                cache["prices"][s] = m
        cache["at"] = time.monotonic()
    mgr._record_equity(sid)
