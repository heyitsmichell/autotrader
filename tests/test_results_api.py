"""Results tab API (app/api/results.py, app/results.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_api_support import HDR, make_app, make_client

NOW = datetime.now(timezone.utc)


def iso(mins_ago):
    return (NOW - timedelta(minutes=mins_ago)).isoformat(timespec="seconds")


def day(n):
    return (NOW.date() - timedelta(days=3 - n)).isoformat()   # n=0..3 (4 days)


def ledger(db, sym, **kw):
    f = {"symbol": sym, "side": "BUY", "quantity_requested": 10, "quantity_filled": 10, "limit_price": 100.0,
         "status": "open", "created_at": iso(500), "broker_mode": db.broker_mode,
         "webull_environment": db.webull_environment, "account_id": db.account_id}
    f.update(kw)
    db._insert("trade_ledger", f)


def seed(db):
    ledger(db, "AAA", entry_fill_price=100.0, entry_fill_time=iso(300))
    ledger(db, "BBB", entry_fill_price=50.0, entry_fill_time=iso(200), exit_fill_price=55.0,
           exit_fill_time=iso(100), status="closed", exit_reason="target")
    ledger(db, "CCC", entry_fill_price=10.0, entry_fill_time=iso(250), exit_fill_price=12.0,
           exit_fill_time=iso(90), status="closed", realized_pnl=17.5)
    a = db.add_sleeve_order("momentum", "MSFT", "stock", "BUY", 5, 200.0, "c1")
    db.update_sleeve_order(a, status="filled", filled_qty=5, avg_fill_price=201.0)
    b = db.add_sleeve_order("momentum", "NVDA", "stock", "BUY", 5, 100.0, "c2")
    db.update_sleeve_order(b, status="rejected")
    c = db.add_sleeve_order("putwrite", "SPY260101P00500000", "option", "SELL", 2, 3.0, "c3")
    db.update_sleeve_order(c, status="filled", filled_qty=2, avg_fill_price=3.0)
    db.add_shadow_trade("vaa_g4", "SPY", "BUY", 3, 400.0, "top")
    db.add_shadow_trade("mom_skip1w", "AAPL", "BUY", 2, 150.0)
    eq = [(100, 1000, 500, None, 2000), (110, 1005, 520, None, 2050),
          (120, 1010, None, 700, 2040), (150, 1000, 530, 710, 2100)]
    for n, vals in enumerate(eq):
        for k, x in zip(("swing", "momentum", "putwrite", "vaa", "total"), vals):
            if x is not None:
                db.record_sleeve_equity(day(n), k, float(x))
    for n, (a_, b_) in enumerate([(100, 100), (101, 99), (103, None), (102, 99)]):
        db.record_shadow_equity(day(n), "vaa_g4", float(a_))
        if b_ is not None:
            db.record_shadow_equity(day(n), "mom_skip1w", float(b_))


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path)
    seed(a.state.db)
    yield a
    a.state.db.close()


async def get(app, url):
    async with make_client(app) as c:
        return await c.get(url, headers=HDR)


async def test_trades_all_shape_order(app):
    j = (await get(app, "/api/results/trades")).json()
    rows = j["trades"]
    assert [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True)
    assert set(rows[0]) == {"ts", "strategy", "live", "symbol", "kind", "side", "qty", "price", "notional",
                            "status", "pnl", "reason", "source"}
    assert j["counts"] == {"swing": 5, "momentum": 1, "putwrite": 1, "vaa_g4": 1, "mom_skip1w": 1, "all": 9, "shadows": 2}
    sh = [r for r in rows if r["source"] == "shadow"]
    assert len(sh) == 2 and not any(r["live"] for r in sh)


async def test_swing_pnl_and_open(app):
    rows = (await get(app, "/api/results/trades?strategy=swing")).json()["trades"]
    by = {(r["symbol"], r["side"]): r for r in rows}
    assert by[("AAA", "BUY")]["status"] == "open" and len(rows) == 5
    assert by[("BBB", "SELL")]["pnl"] == 50.0           # computed (55-50)*10
    assert by[("CCC", "SELL")]["pnl"] == 17.5           # stored realized_pnl wins
    assert by[("BBB", "SELL")]["reason"] == "target"
    assert by[("BBB", "BUY")]["pnl"] is None and by[("AAA", "BUY")]["notional"] == 1000.0


async def test_include_failed_and_option_notional(app):
    r = (await get(app, "/api/results/trades?strategy=momentum")).json()["trades"]
    assert [x["symbol"] for x in r] == ["MSFT"] and r[0]["notional"] == 1005.0
    r = (await get(app, "/api/results/trades?strategy=momentum&include_failed=1")).json()["trades"]
    assert {x["status"] for x in r} == {"filled", "rejected"}
    o = (await get(app, "/api/results/trades?strategy=putwrite")).json()["trades"][0]
    assert o["kind"] == "option" and o["notional"] == 600.0 and o["pnl"] is None


async def test_shadow_filters_and_limit(app):
    j = (await get(app, "/api/results/trades?strategy=shadows")).json()
    assert j["counts"]["shadows"] == 2 and j["counts"]["vaa_g4"] == 1 and j["counts"]["swing"] == 5   # counts cover ALL strategies
    j = (await get(app, "/api/results/trades?strategy=vaa_g4")).json()
    assert [r["symbol"] for r in j["trades"]] == ["SPY"]
    assert len((await get(app, "/api/results/trades?limit=2")).json()["trades"]) == 2


@pytest.mark.parametrize("q", ["strategy=bogus", "days=0", "days=3651", "limit=0", "limit=2001"])
async def test_bad_params(app, q):
    assert (await get(app, f"/api/results/trades?{q}")).status_code == 400


async def test_pnl_bad_days(app):
    assert (await get(app, "/api/results/pnl?days=0")).status_code == 400


async def test_pnl(app):
    j = (await get(app, "/api/results/pnl")).json()
    assert [d["swing"] for d in j["days"]] == [0.0, 10.0, 10.0, 30.0]
    assert [d["momentum"] for d in j["days"]] == [0.0, 5.0, 5.0, -10.0]
    assert [d["putwrite"] for d in j["days"]] == [0.0, 20.0, None, 10.0]   # diff vs last recorded
    assert [d["vaa"] for d in j["days"]] == [None, None, 0.0, 10.0]
    assert [d["total"] for d in j["days"]] == [0.0, 50.0, -10.0, 60.0]
    assert [d["swing"] for d in j["cumulative"]] == [0.0, 10.0, 20.0, 50.0]
    assert j["cumulative"][2]["putwrite"] is None
    p = j["periods"]
    assert p["today"]["swing"] == 30.0 and p["week"]["total"] == 100.0
    assert p["since_start"]["putwrite"] == 30.0 and p["month"]["vaa"] == 10.0
    sh = j["shadows"]
    assert [d["vaa_g4"] for d in sh["days"]] == [0.0, 1.0, 2.0, -1.0]
    assert sh["days"][2]["mom_skip1w"] is None and sh["days"][3]["mom_skip1w"] == 0.0
    assert sh["periods"]["since_start"]["vaa_g4"] == 2.0
    assert sh["periods"]["since_start"]["swing_dynamic"] is None


async def test_namespace_isolation(app):
    db = app.state.db
    db.conn.execute("INSERT INTO shadow_trades (ns, ts, shadow, symbol, side, qty, price) VALUES "
                    "('live:prod:X', ?, 'vaa_g4', 'ZZZ', 'BUY', 1, 1)", (iso(1),))
    db.conn.execute("INSERT INTO sleeve_orders (ns, ts, sleeve, symbol, kind, side, qty, status, filled_qty) "
                    "VALUES ('live:prod:X', ?, 'vaa', 'ZZZ', 'stock', 'BUY', 1, 'filled', 1)", (iso(1),))
    db.conn.execute("INSERT INTO sleeve_equity (day, sleeve, ns, equity) VALUES (?, 'swing', 'live:prod:X', 9)",
                    (day(3),))
    ledger(db, "ZZZ", entry_fill_price=1.0, entry_fill_time=iso(1), broker_mode="live",
           webull_environment="prod", account_id="X")
    rows = (await get(app, "/api/results/trades")).json()["trades"]
    assert "ZZZ" not in {r["symbol"] for r in rows}
    j = (await get(app, "/api/results/pnl")).json()
    assert j["days"][-1]["swing"] == 30.0


def test_pnl_includes_shadow_display_names(tmp_path):
    from app.results import pnl
    from app.db import DB
    db = DB(tmp_path / "t.db", "mock", None, None)
    names = pnl(db, 30)["shadows"]["names"]
    assert names.get("swing_dynamic") and names["swing_dynamic"] != "swing_dynamic"


# ---------------------------------------------------------------- round trips (FIFO) + stats

def order(db, ts, sleeve, sym, side, qty, px, kind="stock", status="filled", reason=None):
    db.conn.execute("INSERT INTO sleeve_orders (ns, ts, sleeve, symbol, kind, side, qty, status, filled_qty, "
                    "avg_fill_price, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (db._sleeve_ns, ts, sleeve, sym, kind, side, qty, status,
                     qty if status == "filled" else 0, px, reason))


async def test_round_trips_swing_and_stats(app):
    j = (await get(app, "/api/results/round_trips")).json()
    sw = [t for t in j["trips"] if t["strategy"] == "swing"]
    assert {t["symbol"]: t["pnl"] for t in sw} == {"BBB": 50.0, "CCC": 17.5}
    assert next(t for t in sw if t["symbol"] == "BBB")["ret_pct"] == 10.0
    s = j["stats"]["swing"]
    assert s["n"] == 2 and s["wins"] == 2 and s["win_rate"] == 1.0 and s["profit_factor"] is None
    assert s["total"] == 67.5 and s["best"]["symbol"] == "BBB" and s["exits"]["other"]["n"] == 2
    assert j["stats"]["live"]["n"] == 2 and j["stats"]["shadows"]["n"] == 0
    assert (await get(app, "/api/results/round_trips?days=0")).status_code == 400


async def test_fifo_partial_sell_and_fill_pnl(app):
    db = app.state.db
    order(db, iso(80), "vaa", "IEF", "BUY", 10, 100.0)
    order(db, iso(70), "vaa", "IEF", "BUY", 10, 110.0)
    order(db, iso(60), "vaa", "IEF", "SELL", 15, 120.0, reason="rebalance")
    trips = [t for t in (await get(app, "/api/results/round_trips")).json()["trips"] if t["strategy"] == "vaa"]
    assert sorted((t["qty"], t["entry_price"], t["pnl"]) for t in trips) == [(5.0, 110.0, 50.0), (10.0, 100.0, 200.0)]
    assert {t["bucket"] for t in trips} == {"rebalance"}
    rows = (await get(app, "/api/results/trades?strategy=vaa")).json()["trades"]
    assert [r["pnl"] for r in rows if r["side"] == "SELL"] == [250.0]
    assert all(r["pnl"] is None for r in rows if r["side"] == "BUY")


async def test_put_expiry_and_assignment(app):
    db = app.state.db
    old = (NOW - timedelta(days=200)).isoformat(timespec="seconds")
    exp = (NOW - timedelta(days=170)).date()
    occ = f"QQQ{exp:%y%m%d}P00500000"
    order(db, old, "putwrite", occ, "SELL", 2, 4.0, kind="option")
    order(db, iso(50), "putwrite", "QQQ", "SELL", 200, 490.0, reason="assigned shares")
    j = (await get(app, "/api/results/round_trips")).json()
    pw = {t["kind"]: t for t in j["trips"] if t["strategy"] == "putwrite" and t["symbol"].startswith("QQQ")}
    assert pw["option"]["pnl"] == 800.0 and pw["option"]["exit_reason"] == "expired"
    assert pw["option"]["ret_pct"] == 0.8                     # 800 / (500 x 100 x 2) collateral
    assert pw["stock"]["entry_price"] == 500.0 and pw["stock"]["pnl"] == -2000.0
    assert j["stats"]["putwrite"]["exits"]["expiry"]["n"] == 1


async def test_held_put_is_not_expired(app):
    db = app.state.db
    exp = (NOW - timedelta(days=1)).date()
    occ = f"QQQ{exp:%y%m%d}P00500000"
    order(db, (NOW - timedelta(days=30)).isoformat(timespec="seconds"), "putwrite", occ, "SELL", 1, 4.0, kind="option")
    db.set_sleeve_holding("putwrite", occ, "option", -1, 4.0)
    j = (await get(app, "/api/results/round_trips")).json()
    assert not [t for t in j["trips"] if t["symbol"] == occ]


async def test_shadow_round_trip(app):
    db = app.state.db
    db.add_shadow_trade("vaa_g4", "SPY", "SELL", 3, 420.0, "rebalance")
    j = (await get(app, "/api/results/round_trips")).json()
    t = next(t for t in j["trips"] if t["strategy"] == "vaa_g4")
    assert t["pnl"] == 60.0 and t["live"] is False and t["ret_pct"] == 5.0
    assert j["stats"]["shadows"]["n"] == 1 and j["stats"]["live"]["n"] == 2


async def test_unmatched_stock_sell_opens_no_short(app):
    db = app.state.db
    order(db, iso(80), "momentum", "ZZZ", "SELL", 5, 50.0, reason="rebalance")   # history before the log
    order(db, iso(70), "momentum", "ZZZ", "BUY", 5, 40.0, reason="rebalance")
    j = (await get(app, "/api/results/round_trips")).json()
    assert not [t for t in j["trips"] if t["symbol"] == "ZZZ"]
