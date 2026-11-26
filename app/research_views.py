"""Read-only per-strategy research views (docs/RESEARCH_TAB.md "Backend").

Pure helpers plus cached Yahoo loaders. Nothing here places or cancels an order. Every public
``build_*`` coroutine returns ``{"as_of": iso, ...}`` and never raises on a data problem: it returns
``{"as_of", "error": "plain sentence"}`` instead.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from datetime import date, datetime

import pandas as pd

from .broker import BrokerError
from .db import decode
from .engine.util import parse_occ
from .market import now_et
from .portfolio import parse_position_rows
from .sleeves import data, registry
from .sleeves.manager import VAA_SYMBOLS, next_rebalance_day
from .sleeves.strategies import vaa

log = logging.getLogger("app.research_views")
CACHE_SECONDS = 60 * 60
_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def clear_cache() -> None:
    with _GUARD:
        _CACHE.clear()


# ----------------------------------------------------------------------------- Yahoo loaders (blocking)
def load_sp500_closes() -> pd.DataFrame:
    return data.daily_closes(data.sp500_symbols())


def load_vaa_closes() -> pd.DataFrame:
    return data.daily_closes(VAA_SYMBOLS)


def load_market_closes() -> pd.DataFrame:
    return data.daily_closes(["SPY", "^VIX"])


_LOADERS = {"sp500": "load_sp500_closes", "vaa": "load_vaa_closes", "market": "load_market_closes"}


def _load_blocking(name: str) -> pd.DataFrame:
    with _GUARD:
        lock = _LOCKS.setdefault(name, threading.Lock())
    with lock:                      # concurrent requests share one download
        hit = _CACHE.get(name)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        df = globals()[_LOADERS[name]]()     # looked up at call time so tests can patch it
        if df is not None and not df.empty:
            _CACHE[name] = (time.monotonic(), df)
        return df


async def closes(name: str) -> pd.DataFrame:
    """Cached daily closes ('sp500' | 'vaa' | 'market'); empty frame on any failure."""
    try:
        df = await asyncio.to_thread(_load_blocking, name)
    except Exception as e:  # noqa: BLE001 - a Yahoo outage must never be a 500
        log.warning("research view: %s download failed: %s", name, e)
        return pd.DataFrame()
    return df if df is not None else pd.DataFrame()


async def warm_cache(delay: float = 30.0, names: tuple[str, ...] = ("sp500", "market", "vaa"),
                     every: float | None = None) -> None:
    """Background warm-up of the Yahoo close caches behind Momentum (slow, ~35 s cold), Regime and VAA.
    With ``every`` set it repeats (just under CACHE_SECONDS) so the views stay warm, not only for
    the first hour after a restart. Never raises (except cancellation); downloads run in threads."""
    if delay > 0:
        await asyncio.sleep(delay)
    while True:
        try:
            for name in names:
                t0 = time.monotonic()
                df = await closes(name)          # already swallows download errors
                log.info("research warm-up: %s %s in %.1fs", name, "ok" if not df.empty else "empty",
                         time.monotonic() - t0)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - warm-up is best effort
            log.warning("research warm-up failed: %s", e)
        if not every:
            return
        await asyncio.sleep(every)


def _asof() -> str:
    return now_et().isoformat(timespec="seconds")


def _err(msg: str) -> dict:
    return {"as_of": _asof(), "error": msg}


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


# ----------------------------------------------------------------------------- shared reads
def _stock_holdings(db, sid: str) -> dict[str, float]:
    try:
        return {h["symbol"].upper(): float(h["qty"]) for h in db.sleeve_holdings(sid) if h["kind"] == "stock"}
    except Exception:  # noqa: BLE001
        return {}


def _next_rebalance(db, sid: str) -> str:
    """Same rule as SleeveManager._sleeve_view: first trading day of the month after the last one."""
    try:
        last = (db.sleeve_state(sid) or {}).get("last_rebalance")
        if last:
            return next_rebalance_day(datetime.fromisoformat(last).date()).isoformat()
    except Exception:  # noqa: BLE001
        pass
    return now_et().date().isoformat()


# ----------------------------------------------------------------------------- momentum
def momentum_rows(cl: pd.DataFrame, n_target: int, held: dict[str, float], top: int = 25) -> tuple[list[dict], int]:
    # same score as strategies/momentum.py: close[t-21] / close[t-252] - 1 on the last row
    score = (cl.shift(21) / cl.shift(252) - 1).iloc[-1].dropna()
    ranked = score.sort_values(ascending=False, kind="stable")
    cutoff = float(ranked.iloc[n_target - 1]) if len(ranked) >= n_target else None
    rows = []
    for i, (sym, sc) in enumerate(ranked.iloc[:top].items(), 1):
        q = held.get(str(sym))
        rows.append({"rank": i, "symbol": str(sym), "score": float(sc), "held": bool(q), "held_qty": q or 0.0,
                     "in_target": i <= n_target,
                     "gap_to_cutoff": float(sc) - cutoff if cutoff is not None else None})
    return rows, len(ranked)


async def build_momentum(db, settings) -> dict:
    n = int(getattr(settings, "sleeve_momentum_n", 10) or 10)
    cl = await closes("sp500")
    if cl.empty or len(cl) < 253:
        return _err("Not enough S&P 500 price history is available right now (Yahoo Finance). Try again later.")
    rows, universe = momentum_rows(cl, n, _stock_holdings(db, "momentum"))
    if universe < n:
        return _err("Too few S&P 500 stocks have a full year of prices to rank.")
    return {"as_of": _asof(), "n_target": n, "rows": rows, "next_rebalance": _next_rebalance(db, "momentum"),
            "universe_n": universe}


# ----------------------------------------------------------------------------- VAA
async def build_vaa(db) -> dict:
    cl = await closes("vaa")
    need = list(dict.fromkeys(vaa.G12 + vaa.DEFENSIVE))
    if cl.empty or any(s not in cl.columns for s in need):
        return _err("Price history for the VAA funds is not available right now (Yahoo Finance). Try again later.")
    mo = {s: float(vaa.m13612w(cl[s])) for s in need}
    if any(math.isnan(x) for x in mo.values()):
        return _err("The VAA funds do not have a full year of prices yet.")
    order = sorted(vaa.G12, key=lambda s: mo[s], reverse=True)
    offensive = [{"symbol": s, "score": mo[s], "rank": i + 1, "bad": mo[s] <= 0} for i, s in enumerate(order)]
    bad = sum(1 for o in offensive if o["bad"])
    vd = {s: mo[s] for s in vaa.DEFENSIVE}
    return {"as_of": _asof(), "B": vaa.B, "bad": bad, "cash_fraction": min(1.0, bad / vaa.B),
            "offensive": offensive, "defensive": [{"symbol": s, "score": vd[s]} for s in vaa.DEFENSIVE],
            "best_defensive": max(vd, key=vd.get), "allocation_if_today": {k: float(v) for k, v in vaa.targets(cl).items()},
            "held": _stock_holdings(db, "vaa"), "next_rebalance": _next_rebalance(db, "vaa")}


# ----------------------------------------------------------------------------- put-write
def _mark(snap: dict | None) -> float | None:
    if not snap:
        return None
    bid, ask = _f(snap.get("bid")), _f(snap.get("ask"))
    if bid is not None and ask is not None and ask > 0:
        return (bid + ask) / 2
    return _f(snap.get("last"))


async def build_putwrite(db, broker, settings) -> dict:
    under = getattr(settings, "sleeve_putwrite_underlying", "QQQ")
    today = now_et().date()
    holds = [h for h in db.sleeve_holdings("putwrite") if h["kind"] == "option" and float(h["qty"]) < 0]
    try:
        spot = _f(await broker.last_price(under))
    except BrokerError:
        spot = None
    snaps: dict = {}
    if holds:
        try:
            snaps = await broker.parsed_option_snapshots([h["symbol"] for h in holds]) or {}
        except BrokerError:
            snaps = {}
    puts = []
    for h in holds:
        occ = parse_occ(h["symbol"])
        if not occ:
            continue
        n = int(abs(float(h["qty"])))
        strike = occ["strike"]
        exp = date.fromisoformat(occ["expiration"])
        snap = snaps.get(h["symbol"]) or snaps.get(h["symbol"].upper())
        mark = _mark(snap)
        cost = _f(h.get("avg_cost"))
        premium = cost * 100 * n if cost is not None else None
        delta = _f((snap or {}).get("delta"))
        puts.append({
            "symbol": h["symbol"], "strike": strike, "expiration": occ["expiration"], "dte": (exp - today).days,
            "contracts": n, "premium_received": round(premium, 2) if premium is not None else None,
            "mark": mark,
            "unrealized_pnl": round(premium - mark * 100 * n, 2) if premium is not None and mark is not None else None,
            "delta": delta, "iv": _f((snap or {}).get("iv")),
            "prob_itm": abs(delta) if delta is not None else None,
            "distance_pct": (spot - strike) / spot if spot else None,
            "collateral": strike * 100 * n,
            "next_action": (f"Held to expiry on {occ['expiration']}; bought back that afternoon only if "
                            f"{occ['underlying']} is below {strike:g}"),
        })
    st = db.sleeve_state("putwrite") or {}
    return {"as_of": _asof(), "underlying": under, "spot": spot, "puts": puts,
            "cash": _f(st.get("cash")), "rule": registry.get("putwrite").rebalance_rule}


# ----------------------------------------------------------------------------- overlap
def _shadow_ids() -> list[str]:
    try:
        from .shadows import registry as sreg
        return [s.id for s in sreg.SHADOWS]
    except Exception:  # noqa: BLE001
        return []


def overlap_rows(holders_by_symbol: dict[str, list[dict]], net_liq: float | None, live_only: bool) -> list[dict]:
    out = []
    for sym, hs in holders_by_symbol.items():
        live = [h for h in hs if h["live"]]
        if live_only:                                  # held by 2+ live strategies
            ok = len({h["strategy"] for h in live}) > 1
        else:                                          # a live holder AND a shadow holder
            ok = bool(live) and len(live) < len(hs)
        if not ok:
            continue
        total = sum(h["value"] for h in hs)
        out.append({"symbol": sym, "total_value": round(total, 2),
                    "pct_of_net": round(total / net_liq, 4) if net_liq else None, "holders": hs})
    return sorted(out, key=lambda r: -r["total_value"])


async def build_overlap(db, broker, portfolio) -> dict:
    last: dict[str, float] = {}
    try:
        text = await broker.positions_text()
        last = {r["symbol"]: float(r["last"]) for r in parse_position_rows(text or "") if r.get("last")}
    except Exception:  # noqa: BLE001
        pass
    net_liq, swing = None, []
    if portfolio is not None:
        try:
            snap = await portfolio.snapshot()
            net_liq = _f((snap.get("balance") or {}).get("net_liq"))
            swing = snap.get("positions") or []
        except Exception:  # noqa: BLE001
            pass

    by_sym: dict[str, list[dict]] = {}

    def add(sym: str, strategy: str, live: bool, qty: float, px: float | None, cost: float | None) -> None:
        px = px if px is not None else (cost or 0.0)
        by_sym.setdefault(sym.upper(), []).append(
            {"strategy": strategy, "live": live, "qty": qty, "value": round(qty * px, 2)})

    for r in swing:
        q = _f(r.get("qty")) or 0.0
        if q > 0:
            mv = _f(r.get("market_value"))
            add(r["symbol"], "swing", True, q, (mv / q) if mv is not None else _f(r.get("last")), _f(r.get("cost")))
    for h in db.sleeve_holdings():
        if h["kind"] == "stock" and float(h["qty"]) > 0:
            add(h["symbol"], h["sleeve"], True, float(h["qty"]), last.get(h["symbol"].upper()), _f(h.get("avg_cost")))
    live_by = {k: list(v) for k, v in by_sym.items()}
    for sid in _shadow_ids():
        try:
            for h in db.shadow_holdings(sid):
                if float(h["qty"]) > 0:
                    add(h["symbol"], sid, False, float(h["qty"]), last.get(h["symbol"].upper()),
                        _f(h.get("avg_cost")))
        except Exception:  # noqa: BLE001
            continue
    return {"as_of": _asof(), "net_liq": net_liq,
            "rows": overlap_rows(live_by, net_liq, True),
            "shadow_overlap": overlap_rows(by_sym, net_liq, False)}


# ----------------------------------------------------------------------------- regime
def gate_summary(signals: list[dict]) -> dict:
    """Same counting as web/src/components/today/gate.ts summarizeGate."""
    counts: dict[str, int] = {}
    eligible = passed = 0
    for s in signals:
        checks = s.get("consensus_checks") or []
        if not checks:
            continue
        if checks[0].get("ok"):
            eligible += 1
        fail = next((c for c in checks if not c.get("ok")), None)
        if fail is None:
            passed += 1
            continue
        counts[fail.get("name")] = counts.get(fail.get("name"), 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    return {"watched": len(signals), "eligible": eligible, "pass": passed,
            "top_blockers": [{"name": k, "n": v} for k, v in top]}


async def build_regime(db) -> dict:
    mk = await closes("market")
    if mk.empty or "SPY" not in mk.columns or mk["SPY"].dropna().empty:
        return _err("SPY price history is not available right now (Yahoo Finance). Try again later.")
    spy = mk["SPY"].dropna()
    last = float(spy.iloc[-1])
    sma50 = float(spy.tail(50).mean()) if len(spy) >= 50 else None
    sma200 = float(spy.tail(200).mean()) if len(spy) >= 200 else None
    spy_o = {"last": last, "sma50": sma50, "sma200": sma200,
             "above_200": bool(sma200 is not None and last > sma200),
             "ret_1m": last / float(spy.iloc[-22]) - 1 if len(spy) > 21 else None,
             "ret_3m": last / float(spy.iloc[-64]) - 1 if len(spy) > 63 else None}
    vix_o = None
    if "^VIX" in mk.columns and not mk["^VIX"].dropna().empty:
        v = mk["^VIX"].dropna().tail(252)
        vix_o = {"last": float(v.iloc[-1]), "pct_rank_1y": float((v <= v.iloc[-1]).mean())}
    breadth = None
    sp = await closes("sp500")
    if not sp.empty:
        series = {c: sp[c].dropna() for c in sp.columns}
        ok = [c for c, s in series.items() if len(s) >= 200]
        if ok:
            above = sum(1 for c in ok if series[c].iloc[-1] > series[c].tail(200).mean())
            breadth = {"pct_above_200": above / len(ok), "n": len(ok)}
    vb = await build_vaa(db)
    try:
        sigs = [decode(r) for r in db.latest_signals()]
    except Exception:  # noqa: BLE001
        sigs = []
    return {"as_of": _asof(), "spy": spy_o, "vix": vix_o, "breadth": breadth,
            "vaa_bad": vb.get("bad"), "swing_gate": gate_summary(sigs)}
