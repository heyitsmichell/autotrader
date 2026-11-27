"""Gather today's holdings, prices and IV from the running engine. Read-only broker calls only."""
from __future__ import annotations

import logging
from datetime import date

from ..engine.util import parse_occ
from ..market import now_et
from ..portfolio_parsing import parse_position_rows
from ..sleeves import registry

log = logging.getLogger("trader.risk_report")


def _f(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _iv(x):
    v = _f(x)
    if not v or v <= 0:
        return None
    return v / 100.0 if v > 3 else v           # snapshots may quote percent


def _option(sym: str, sleeve: str, qty: float, cost, mark, spot) -> dict | None:
    occ = parse_occ(sym)
    if not occ:
        return None
    exp = date.fromisoformat(occ["expiration"])
    return {"symbol": sym.upper(), "kind": "option", "sleeve": sleeve, "qty": qty, "underlying": occ["underlying"],
            "right": "P" if occ["option_type"] == "PUT" else "C", "strike": occ["strike"],
            "expiry": occ["expiration"], "dte": (exp - now_et().date()).days, "spot": spot,
            "mark": mark, "cost": cost, "iv": None, "iv_source": None}


def _stock(sym: str, sleeve: str, qty: float, price) -> dict:
    return {"symbol": sym.upper(), "kind": "stock", "sleeve": sleeve, "qty": qty, "spot": price}


async def gather(eng) -> dict:
    """{holdings, sleeves: {id: {name, equity, cash}}, net_liq, warnings, series}."""
    from ..engine.option_exits import parse_held_options
    warnings: list[str] = []
    mgr = getattr(eng, "sleeves", None)
    if mgr is None:                         # read-only: never construct a manager here
        from types import SimpleNamespace
        mgr = SimpleNamespace(last_values={}, db=eng.db, _value=None)
        warnings.append("sleeve manager unavailable; sleeve equity not valued")
    values = mgr.last_values or {}
    text = ""
    try:
        text = await eng.broker.positions_text()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"positions unavailable: {exc}")
    text = text or ""
    if text.startswith("No data"):
        text = ""
    if not values and getattr(mgr, "_value", None) is not None:
        try:
            values = mgr._value(await mgr._snapshot())
        except Exception as exc:  # noqa: BLE001
            log.warning("risk: sleeve valuation failed: %s", exc)
            warnings.append("sleeve valuation unavailable")
            values = {}
    rows = {r["symbol"]: r for r in parse_position_rows(text)}
    try:
        held = {h.symbol.upper(): h for h in parse_held_options(text)}
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"option positions unreadable: {exc}")
        held = {}

    def spot_of(sym):
        return _f((rows.get(sym) or {}).get("last"))

    holdings: list[dict] = []
    sleeve_stock_qty: dict[str, float] = {}
    sleeve_opt_qty: dict[str, float] = {}
    for sid in registry.NON_SWING:
        try:
            hs = mgr.db.sleeve_holdings(sid)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{sid} holdings unavailable: {exc}")
            continue
        for h in hs:
            q = float(h["qty"])
            if h["kind"] == "option":
                o = held.get(h["symbol"].upper())
                rec = _option(h["symbol"], sid, q, _f(h.get("avg_cost")), _f(getattr(o, "mark", None)), None)
                if rec:
                    holdings.append(rec)
                    sleeve_opt_qty[rec["symbol"]] = sleeve_opt_qty.get(rec["symbol"], 0.0) + q
            else:
                sym = h["symbol"].upper()
                px = spot_of(sym)
                if px is None:
                    px = _f(h.get("avg_cost"))
                    warnings.append(f"no live price for {sym}; used average cost")
                holdings.append(_stock(sym, sid, q, px))
                sleeve_stock_qty[sym] = sleeve_stock_qty.get(sym, 0.0) + q
    # swing: broker positions with the other sleeves' shares removed
    cache = getattr(getattr(eng, "portfolio", None), "_cache", None) or {}
    pos = cache.get("positions")
    if pos:
        swing = [(r["symbol"].upper(), float(r["qty"]), _f(r.get("last"))) for r in pos]
    else:
        swing = [(s, r["qty"] - sleeve_stock_qty.get(s, 0.0), _f(r.get("last"))) for s, r in rows.items()]
    for sym, q, last in swing:
        if q > 1e-9:
            holdings.append(_stock(sym, "swing", q, last))
    for sym, o in held.items():
        q = o.qty - sleeve_opt_qty.get(sym, 0.0)
        if abs(q) > 1e-9:
            rec = _option(sym, "swing", q, _f(o.cost), _f(o.mark), None)
            if rec:
                holdings.append(rec)
    opts = [h for h in holdings if h["kind"] == "option"]
    try:
        syms = sorted({h["symbol"] for h in opts})
        snaps: dict = {}
        for i in range(0, len(syms), 20):
            snaps.update(await eng.broker.parsed_option_snapshots(syms[i:i + 20]))
        for h in opts:
            s = snaps.get(h["symbol"]) or {}
            h["iv"] = _iv(s.get("iv"))
            h["iv_source"] = "snapshot" if h["iv"] else None
            if h["mark"] is None:
                bid, ask, last = _f(s.get("bid")), _f(s.get("ask")), _f(s.get("last"))
                h["mark"] = (bid + ask) / 2 if (bid and ask) else (last or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("risk: option snapshots failed: %s", exc)
        warnings.append("option snapshots unavailable; IV estimated from realized vol")
    for h in holdings:                      # an option's underlying price when the account also holds the stock
        if h["kind"] == "option" and h["spot"] is None:
            h["spot"] = spot_of(h["underlying"])
    for u in sorted({h["underlying"] for h in holdings if h["kind"] == "option" and h["spot"] is None}):
        try:                                # read-only quote; prepare() falls back to the last close
            px = await eng.broker.last_price(u)
        except Exception as exc:  # noqa: BLE001
            log.warning("risk: last_price(%s) failed: %s", u, exc)
            px = None
        for h in holdings:
            if h["kind"] == "option" and h["underlying"] == u and h["spot"] is None:
                h["spot"] = _f(px)
    try:
        series = mgr.db.sleeve_equity_series(400)
    except Exception:  # noqa: BLE001
        series = []
    sleeves = {d.id: {"name": d.name, "equity": _f((values.get(d.id) or {}).get("equity")),
                      "cash": _f((values.get(d.id) or {}).get("cash"))} for d in registry.SLEEVES}
    return {"holdings": holdings, "sleeves": sleeves, "net_liq": _f((values.get("total") or {}).get("equity")),
            "warnings": warnings, "series": series}
