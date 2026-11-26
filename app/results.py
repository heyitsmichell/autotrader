"""Read-only query/aggregation helpers for the Results tab (docs/RESULTS.md)."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from statistics import median

from .engine.util import parse_occ
from .shadows import registry as shadow_registry

SLEEVES = ("swing", "momentum", "putwrite", "vaa")
SERIES = (*SLEEVES, "total")
STRATEGIES = ("all", *SLEEVES, "shadows", *shadow_registry.IDS)
FAILED = ("rejected", "cancelled", "canceled", "failed")
PERIODS = {"today": 1, "week": 5, "month": 21, "since_start": None}


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _r2(v):
    return None if v is None else round(float(v), 2)


def _num(v):
    return None if v is None else float(v)


def _row(ts, strategy, live, symbol, kind, side, qty, price, status, pnl, reason, source) -> dict:
    qty = _num(qty)
    price = _num(price)
    mult = 100 if kind == "option" else 1
    notional = round(qty * price * mult, 2) if qty is not None and price is not None else None
    return {"ts": ts, "strategy": strategy, "live": live, "symbol": symbol, "kind": kind, "side": side,
            "qty": qty, "price": price, "notional": notional, "status": status, "pnl": _r2(pnl),
            "reason": reason, "source": source}


def swing_rows(db, days: int) -> list[dict]:
    """Ledger rows: an entry row (BUY) per entry fill and an exit row (SELL) per exit fill.
    Exit pnl = ledger realized_pnl when stored, else (exit-entry)*filled qty for BUY trades, else None."""
    where, args = db._ns_where()
    since = _since(days)
    out = []
    for r in db._rows(f"SELECT * FROM trade_ledger WHERE {where} ORDER BY id", *args):
        qty = r.get("quantity_filled_real")
        if qty is None:
            qty = r.get("quantity_filled")
        if r.get("entry_fill_price") is not None and (r.get("entry_fill_time") or "") >= since:
            out.append(_row(r["entry_fill_time"], "swing", True, r["symbol"], "stock", "BUY", qty,
                            r["entry_fill_price"], r["status"], None, None, "ledger"))
        if r.get("exit_fill_price") is not None and (r.get("exit_fill_time") or "") >= since:
            pnl = r.get("realized_pnl")
            if pnl is None and r["side"] == "BUY" and r.get("entry_fill_price") is not None and qty:
                pnl = (r["exit_fill_price"] - r["entry_fill_price"]) * float(qty)
            out.append(_row(r["exit_fill_time"], "swing", True, r["symbol"], "stock", "SELL", qty,
                            r["exit_fill_price"], r["status"], pnl, r.get("exit_reason"), "ledger"))
    return out


def _sleeve_fills(db) -> list[dict]:
    """Every sleeve order with a fill, full history, oldest first (FIFO needs all of it)."""
    return [r for r in db._rows("SELECT * FROM sleeve_orders WHERE ns=? ORDER BY id", db._sleeve_ns)
            if r["status"] in ("filled", "partial") and float(r.get("filled_qty") or 0) > 0]


def _shadow_fills(db) -> list[dict]:
    return db._rows("SELECT * FROM shadow_trades WHERE ns=? ORDER BY id", db._shadow_ns)


def sleeve_rows(db, sleeve: str | None, days: int, include_failed: bool, sell_pnl: dict | None = None) -> list[dict]:
    """Sleeve fills; a closing fill carries its FIFO-realized P&L (see sleeve_trips)."""
    where, args = ["ns=?", "ts>=?"], [db._sleeve_ns, _since(days)]
    if sleeve:
        where.append("sleeve=?")
        args.append(sleeve)
    if sell_pnl is None:
        sell_pnl = sleeve_trips(db)[1]
    out = []
    for r in db._rows(f"SELECT * FROM sleeve_orders WHERE {' AND '.join(where)} ORDER BY id", *args):
        filled = float(r.get("filled_qty") or 0)
        ok = r["status"] in ("filled", "partial") and filled > 0
        if not ok and not (include_failed and r["status"] in FAILED):
            continue
        qty = filled if ok else r["qty"]
        price = r.get("avg_fill_price") if ok else r.get("limit_price")
        out.append(_row(r["ts"], r["sleeve"], True, r["symbol"], r["kind"], r["side"], qty, price,
                        r["status"], sell_pnl.get(r["id"]) if ok else None, r.get("reason"), "sleeve"))
    return out


def shadow_rows(db, shadow: str | None, days: int, sell_pnl: dict | None = None) -> list[dict]:
    where, args = ["ns=?", "ts>=?"], [db._shadow_ns, _since(days)]
    if shadow:
        where.append("shadow=?")
        args.append(shadow)
    if sell_pnl is None:
        sell_pnl = shadow_trips(db)[1]
    return [_row(r["ts"], r["shadow"], False, r["symbol"], "stock", r["side"], r["qty"], r["price"],
                 "filled", sell_pnl.get(r["id"]), r.get("reason"), "shadow")
            for r in db._rows(f"SELECT * FROM shadow_trades WHERE {' AND '.join(where)} ORDER BY id", *args)]


def trades(db, strategy: str, days: int, include_failed: bool, limit: int) -> dict:
    # every row once, then filter: the counts always cover ALL strategies, so the filter
    # buttons keep their numbers whichever one is selected
    every = (swing_rows(db, days) + sleeve_rows(db, None, days, include_failed, sleeve_trips(db)[1])
             + shadow_rows(db, None, days, shadow_trips(db)[1]))
    shadow_ids = set(shadow_registry.IDS)
    counts: dict[str, int] = {"all": len(every), "shadows": 0}
    for r in every:
        counts[r["strategy"]] = counts.get(r["strategy"], 0) + 1
        if r["strategy"] in shadow_ids:
            counts["shadows"] += 1
    if strategy == "all":
        rows = every
    elif strategy == "shadows":
        rows = [r for r in every if r["strategy"] in shadow_ids]
    else:
        rows = [r for r in every if r["strategy"] == strategy]
    rows.sort(key=lambda r: r["ts"] or "", reverse=True)
    return {"trades": rows[:limit], "counts": counts}


def daily_pnl(series: list[dict], keys, starts: dict | None = None) -> list[dict]:
    """series: [{"day", key: equity}] ascending. Diff vs the previous recorded day of that key.

    The first recorded day diffs against ``starts[key]`` (start capital) when known, else 0.
    """
    out, prev = [], dict(starts or {})
    for row in series:
        d = {"day": row["day"]}
        for k in keys:
            v = row.get(k)
            if v is None:
                d[k] = None
            else:
                d[k] = 0.0 if k not in prev else round(float(v) - prev[k], 2)
                prev[k] = float(v)
        out.append(d)
    return out


def cumulative(days_rows: list[dict], keys) -> list[dict]:
    out, run = [], {k: 0.0 for k in keys}
    for row in days_rows:
        d = {"day": row["day"]}
        for k in keys:
            if row[k] is None:
                d[k] = None
            else:
                run[k] = round(run[k] + row[k], 2)
                d[k] = run[k]
        out.append(d)
    return out


def periods(days_rows: list[dict], keys) -> dict:
    res = {}
    for name, n in PERIODS.items():
        win = days_rows if n is None else days_rows[-n:]
        res[name] = {}
        for k in keys:
            vals = [r[k] for r in win if r[k] is not None]
            res[name][k] = round(sum(vals), 2) if vals else None
    return res


def _block(series, keys, starts: dict | None = None) -> dict:
    d = daily_pnl(series, keys, starts)
    return {"days": d, "cumulative": cumulative(d, keys), "periods": periods(d, keys)}


def _starts(series: list[dict], states: dict, keys) -> dict:
    """Start capital per key, only where the series really begins at the key's start date
    (a window that cuts off earlier history must not be diffed against the start capital)."""
    first = series[0]["day"] if series else None
    out = {}
    for k in keys:
        st = states.get(k) or {}
        cap, sd = st.get("start_capital"), (st.get("start_date") or "")[:10]
        if cap and sd and first and sd >= first:
            out[k] = float(cap)
    return out


def account_start(db) -> float | None:
    """Account starting net liq: the same first benchmark_daily net_liq performance.py reports
    as first_net_liq."""
    for r in db.benchmark_daily():
        if r.get("net_liq") is not None:
            return float(r["net_liq"])
    return None


def swing_start(db, states: dict | None = None) -> float | None:
    """Swing has no start capital of its own: account start net liq minus the other sleeves' start."""
    states = states if states is not None else db.sleeve_states()
    acct = account_start(db)
    if acct is None:
        return None
    others = [float((states.get(k) or {}).get("start_capital") or 0) for k in SLEEVES if k != "swing"]
    return acct - sum(others)


def _with_account_starts(series: list[dict], states: dict, starts: dict, db) -> dict:
    """Add swing and total starts, only when the series begins at the sleeves' start date."""
    others = [k for k in SLEEVES if k != "swing"]
    if not series or not all(k in starts for k in others):
        return starts
    acct = account_start(db)
    if acct is None:
        return starts
    out = dict(starts)
    out.setdefault("swing", acct - sum(starts[k] for k in others))
    out["total"] = acct
    return out


def pnl(db, days: int) -> dict:
    ms, ss = db.sleeve_equity_series(days), db.shadow_equity_series(days)
    states = db.sleeve_states()
    mstarts = _with_account_starts(ms, states, _starts(ms, states, SLEEVES), db)
    shadow_ids = list(shadow_registry.IDS)
    shstates = {i: db.shadow_state(i) for i in shadow_ids}
    main = _block(ms, SERIES, mstarts)
    sh = _block(ss, shadow_ids, _starts(ss, shstates, shadow_ids))
    sh["names"] = {d.id: d.name for d in shadow_registry.SHADOWS} if hasattr(shadow_registry, "SHADOWS") else {}
    return {**main, "shadows": sh}


# ---------------------------------------------------------------- closed round trips + stats
# One "trip" = one closed lot. Swing: a closed trade_ledger row. Sleeves and shadows: fills matched
# FIFO per (strategy, symbol), so a partial rebalance sell closes part of the oldest lot. A put-write
# short put that left the account at expiry (no closing order is recorded) closes at $0 on its expiry
# date; if the sleeve then sells the underlying with no open lot, the strike is the cost basis
# (assignment). ret_pct = P&L / entry value; for a short option, P&L / cash collateral (strike x 100).

def exit_bucket(reason: str | None) -> str:
    s = (reason or "").lower()
    if "stop" in s:
        return "stop"
    if "expir" in s or "dte" in s:
        return "expiry"
    if "rebalanc" in s:
        return "rebalance"
    if "time" in s or "days" in s or "hold" in s:
        return "time"
    if any(w in s for w in ("manual", "flatten", "kill", "user")):
        return "manual"
    return "other"


def _days_between(a: str | None, b: str | None) -> float | None:
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400, 2)
    except (TypeError, ValueError):
        return None


def _trip(strategy, live, symbol, kind, direction, qty, entry_ts, exit_ts, entry_px, exit_px, pnl,
          basis, reason, hold_days=None, ret_pct=None, r_mult=None) -> dict:
    if ret_pct is None and pnl is not None and basis:
        ret_pct = pnl / basis * 100
    return {"strategy": strategy, "live": live, "symbol": symbol, "kind": kind, "direction": direction,
            "qty": _num(qty), "entry_ts": entry_ts, "exit_ts": exit_ts, "entry_price": _num(entry_px),
            "exit_price": _num(exit_px), "pnl": _r2(pnl),
            "ret_pct": None if ret_pct is None else round(float(ret_pct), 3),
            "r": None if r_mult is None else round(r_mult, 3),
            "hold_days": hold_days if hold_days is not None else _days_between(entry_ts, exit_ts),
            "exit_reason": reason, "bucket": exit_bucket(reason)}


def swing_trips(db) -> list[dict]:
    where, args = db._ns_where()
    out = []
    for r in db._rows(f"SELECT * FROM trade_ledger WHERE {where} AND status='closed' ORDER BY id", *args):
        entry, exit_ = r.get("entry_fill_price"), r.get("exit_fill_price")
        qty = r.get("quantity_filled_real") or r.get("quantity_filled") or r.get("quantity_requested")
        if entry is None or exit_ is None or not qty:
            continue
        qty = float(qty)
        sign = 1 if r["side"] == "BUY" else -1
        pnl = r.get("realized_pnl")
        if pnl is None:
            pnl = (exit_ - entry) * qty * sign
        risk = abs(entry - r["stop_price"]) * qty if r.get("stop_price") is not None else 0
        hold = r.get("hold_seconds")
        out.append(_trip("swing", True, r["symbol"], "stock", "long" if sign > 0 else "short", qty,
                         r.get("entry_fill_time"), r.get("exit_fill_time"), entry, exit_, pnl, entry * qty,
                         r.get("exit_reason"), None if hold is None else round(hold / 86400, 2),
                         r.get("realized_pnl_pct"), pnl / risk if risk > 0 else None))
    return out


def _fifo(fills: list[dict], key: str, live: bool, held: set | None = None):
    """fills: rows with id, ts, <key> (strategy id), symbol, kind, side, qty/price or
    filled_qty/avg_fill_price, reason; oldest first. held: {(strategy, symbol)} still in the book
    (enables the synthetic expiry of short options). -> (trips, {fill id: realized P&L of that fill})."""
    today = datetime.now(timezone.utc).date().isoformat()
    events = [dict(f) for f in fills]
    if held is not None:
        for strat, sym in {(f[key], f["symbol"]) for f in fills if f["kind"] == "option"}:
            occ = parse_occ(sym)
            if occ and occ["expiration"] <= today and (strat, sym) not in held:
                events.append({"id": None, "ts": f"{occ['expiration']}T21:00:00+00:00", key: strat,
                               "symbol": sym, "kind": "option", "side": "EXPIRE", "reason": "expired"})
        events.sort(key=lambda e: (e["ts"] or "", e["id"] is None, e["id"] or 0))
    books: dict[tuple, deque] = {}
    assigned: dict[tuple, tuple] = {}         # (strategy, underlying) -> (strike, ts)
    trips, fill_pnl = [], {}
    for e in events:
        strat, sym, kind = e[key], e["symbol"], e["kind"]
        mult = 100 if kind == "option" else 1
        book = books.setdefault((strat, sym), deque())
        if e["side"] == "EXPIRE":
            occ = parse_occ(sym) or {}
            while book:
                q, px, ts = book.popleft()
                if q < 0:                     # short put kept its premium
                    basis = (occ.get("strike") or 0) * -q * 100 or px * -q * mult
                    trips.append(_trip(strat, live, sym, kind, "short", -q, ts, e["ts"], px, 0.0,
                                       px * -q * mult, basis, "expired"))
                    if occ.get("underlying"):
                        assigned[(strat, occ["underlying"])] = (occ["strike"], e["ts"])
                else:                         # long option expired worthless
                    trips.append(_trip(strat, live, sym, kind, "long", q, ts, e["ts"], px, 0.0,
                                       -px * q * mult, px * q * mult, "expired"))
            continue
        filled = e.get("filled_qty")
        qty = float(filled if filled is not None else e.get("qty") or 0)
        price = float(e.get("avg_fill_price") or e.get("price") or e.get("limit_price") or 0)
        sgn = 1 if e["side"] == "BUY" else -1
        left, realized, closed = qty, 0.0, False
        while left > 1e-9 and book and (book[0][0] > 0) != (sgn > 0):
            q, px, ts = book[0]
            take = min(left, abs(q))
            pnl = (price - px) * take * mult * (1 if q > 0 else -1)
            occ = parse_occ(sym) if kind == "option" and q < 0 else None
            basis = occ["strike"] * take * 100 if occ else px * take * mult
            trips.append(_trip(strat, live, sym, kind, "long" if q > 0 else "short", take, ts, e["ts"],
                               px, price, pnl, basis, e.get("reason")))
            realized, closed, left = realized + pnl, True, left - take
            rest = abs(q) - take
            if rest <= 1e-9:
                book.popleft()
            else:
                book[0] = (rest if q > 0 else -rest, px, ts)
        if left > 1e-9 and sgn < 0 and kind == "stock" and not book and (strat, sym) in assigned:
            strike, ts = assigned[(strat, sym)]
            pnl = (price - strike) * left
            trips.append(_trip(strat, live, sym, kind, "long", left, ts, e["ts"], strike, price, pnl,
                               strike * left, e.get("reason") or "assigned shares"))
            realized, closed, left = realized + pnl, True, 0.0
        # sleeves and shadows hold stock long only: a stock sell with nothing to close (history that
        # predates the order log) is not a new short, so it opens nothing; options may open short
        if left > 1e-9 and not (sgn < 0 and kind == "stock"):
            book.append((left * sgn, price, e["ts"]))
        if closed and e.get("id") is not None:
            fill_pnl[e["id"]] = round(realized, 2)
    return trips, fill_pnl


def sleeve_trips(db):
    held = {(h["sleeve"], h["symbol"]) for h in db.sleeve_holdings() if abs(float(h.get("qty") or 0)) > 0}
    return _fifo(_sleeve_fills(db), "sleeve", True, held)


def shadow_trips(db):
    return _fifo([{**r, "kind": "stock"} for r in _shadow_fills(db)], "shadow", False)


def _avg(a: list) -> float | None:
    return round(sum(a) / len(a), 2) if a else None


def trip_stats(trips: list[dict]) -> dict:
    pnl = [t["pnl"] for t in trips if t["pnl"] is not None]
    wins, losses = [v for v in pnl if v > 0], [v for v in pnl if v < 0]
    rets = [t["ret_pct"] for t in trips if t["ret_pct"] is not None]
    holds = [t["hold_days"] for t in trips if t["hold_days"] is not None]
    rs = [t["r"] for t in trips if t["r"] is not None]
    ranked = sorted((t for t in trips if t["pnl"] is not None), key=lambda t: t["pnl"])
    pick = lambda t: {"symbol": t["symbol"], "pnl": t["pnl"], "ret_pct": t["ret_pct"]}  # noqa: E731
    exits: dict[str, dict] = {}
    for t in trips:
        b = exits.setdefault(t["bucket"], {"n": 0, "pnl": 0.0})
        b["n"] += 1
        b["pnl"] = round(b["pnl"] + (t["pnl"] or 0), 2)
    return {
        "n": len(trips), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(pnl), 4) if pnl else None,
        "avg_win": _avg(wins), "avg_loss": _avg(losses), "expectancy": _avg(pnl),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
        "total": round(sum(pnl), 2) if pnl else None,
        "avg_ret_pct": round(sum(rets) / len(rets), 3) if rets else None,
        "median_ret_pct": round(median(rets), 3) if rets else None,
        "avg_hold_days": _avg(holds),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "n_r": len(rs),
        "best": pick(ranked[-1]) if ranked else None, "worst": pick(ranked[0]) if ranked else None,
        "exits": exits,
    }


def round_trips(db, days: int) -> dict:
    """Closed trips whose exit is inside the window, newest exit first, plus stats per strategy,
    'live' (every live strategy together) and 'shadows' (every virtual one)."""
    since = _since(days)
    every = swing_trips(db) + sleeve_trips(db)[0] + shadow_trips(db)[0]
    trips = sorted((t for t in every if (t["exit_ts"] or "") >= since),
                   key=lambda t: t["exit_ts"] or "", reverse=True)
    groups: dict[str, list] = {"live": [], "shadows": []}
    for t in trips:
        groups.setdefault(t["strategy"], []).append(t)
        groups["live" if t["live"] else "shadows"].append(t)
    return {"trips": trips, "stats": {k: trip_stats(v) for k, v in groups.items()}}
