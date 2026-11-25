"""Read-only query/aggregation helpers for the Results tab (docs/RESULTS.md)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def sleeve_rows(db, sleeve: str | None, days: int, include_failed: bool) -> list[dict]:
    where, args = ["ns=?", "ts>=?"], [db._sleeve_ns, _since(days)]
    if sleeve:
        where.append("sleeve=?")
        args.append(sleeve)
    out = []
    for r in db._rows(f"SELECT * FROM sleeve_orders WHERE {' AND '.join(where)} ORDER BY id", *args):
        filled = float(r.get("filled_qty") or 0)
        ok = r["status"] in ("filled", "partial") and filled > 0
        if not ok and not (include_failed and r["status"] in FAILED):
            continue
        qty = filled if ok else r["qty"]
        price = r.get("avg_fill_price") if ok else r.get("limit_price")
        out.append(_row(r["ts"], r["sleeve"], True, r["symbol"], r["kind"], r["side"], qty, price,
                        r["status"], None, r.get("reason"), "sleeve"))
    return out


def shadow_rows(db, shadow: str | None, days: int) -> list[dict]:
    where, args = ["ns=?", "ts>=?"], [db._shadow_ns, _since(days)]
    if shadow:
        where.append("shadow=?")
        args.append(shadow)
    return [_row(r["ts"], r["shadow"], False, r["symbol"], "stock", r["side"], r["qty"], r["price"],
                 "filled", None, r.get("reason"), "shadow")
            for r in db._rows(f"SELECT * FROM shadow_trades WHERE {' AND '.join(where)} ORDER BY id", *args)]


def trades(db, strategy: str, days: int, include_failed: bool, limit: int) -> dict:
    # every row once, then filter: the counts always cover ALL strategies, so the filter
    # buttons keep their numbers whichever one is selected
    every = swing_rows(db, days) + sleeve_rows(db, None, days, include_failed) + shadow_rows(db, None, days)
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


def daily_pnl(series: list[dict], keys) -> list[dict]:
    """series: [{"day", key: equity}] ascending. Diff vs the previous recorded day of that key (first = 0)."""
    out, prev = [], {}
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


def _block(series, keys) -> dict:
    d = daily_pnl(series, keys)
    return {"days": d, "cumulative": cumulative(d, keys), "periods": periods(d, keys)}


def pnl(db, days: int) -> dict:
    main = _block(db.sleeve_equity_series(days), SERIES)
    sh = _block(db.shadow_equity_series(days), list(shadow_registry.IDS))
    sh["names"] = {d.id: d.name for d in shadow_registry.SHADOWS} if hasattr(shadow_registry, "SHADOWS") else {}
    return {**main, "shadows": sh}
