"""SleeveManager (app/sleeves/manager.py) against a fake engine and fake broker."""
from __future__ import annotations

import dataclasses
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.config import load_settings
from app.db import DB
from app.market import set_test_time
from app.sleeves.manager import SleeveManager

ET = ZoneInfo("America/New_York")
MONTH = "2026-12"


def at(day: str, hhmm: str):
    set_test_time(datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ET))


class FakeBroker:
    def __init__(self):
        self.prices: dict[str, float] = {}
        self.options_text = ""
        self.orders: dict[str, dict] = {}          # coid -> {status, filled, px}
        self.placed: list[dict] = []
        self.opt_placed: list[dict] = []
        self.cancelled: list[str] = []
        self.contracts_text = ""
        self.snaps: dict[str, dict] = {}

    async def positions_text(self, fresh=False):
        lines = [f"Symbol: {s} Qty: {q} Last: {self.prices.get(s, 1)} Type: EQUITY"
                 for s, q in getattr(self, "qty", {}).items()]
        for s, p in self.prices.items():
            if s not in getattr(self, "qty", {}):
                lines.append(f"Symbol: {s} Qty: 0 Last: {p} Type: EQUITY")
        lines.append(self.options_text)
        return "\n".join(lines) or "No data"

    def _orders_text(self):
        out = []
        for coid, o in self.orders.items():
            out.append(f"Client Order ID: {coid}\n  Client Order ID: {coid}\n  Status: {o['status']}\n"
                       f"  Filled Quantity: {o['filled']}\n  Average Fill Price: {o['px']}\n")
        return "\n".join(out)

    async def order_history_text(self):
        return self._orders_text()

    async def open_orders_text(self):
        return ""

    async def place_order(self, sym, side, qty, px, coid, trading_session=None):
        self.placed.append(dict(sym=sym, side=side, qty=qty, px=px, coid=coid, session=trading_session))

    async def place_option_single_order(self, sym, side, qty, otype, strike, exp, px, coid,
                                        position_intent=None, allow_when_killed=False):
        self.opt_placed.append(dict(sym=sym, side=side, qty=qty, px=px, coid=coid, intent=position_intent,
                                    allow_when_killed=allow_when_killed))

    async def cancel_order(self, coid):
        self.cancelled.append(coid)

    async def quote(self, sym):
        p = self.prices.get(sym)
        return SimpleNamespace(bid=p, ask=p) if p else None

    async def last_price(self, sym):
        return self.prices.get(sym)

    async def option_contracts(self, syms):
        return self.contracts_text

    async def parsed_option_snapshots(self, syms):
        return {s: self.snaps[s] for s in syms if s in self.snaps}

    def fill(self, coid, qty, px, status="FILLED"):
        self.orders[coid] = {"status": status, "filled": qty, "px": px}


class FakeEngine:
    def __init__(self, s, db, broker, portfolio=None):
        self.s, self.db, self.broker, self.portfolio = s, db, broker, portfolio
        self.kill_switch = False
        self.scheduler_stopped_by_user = False
        self.sched_calls: list[str] = []

    async def stop_scheduler(self):
        self.sched_calls.append("stop")

    def start_scheduler(self):
        self.sched_calls.append("start")


def make_settings(**kw):
    base = dict(sleeves_enabled=True, max_order_usd=50000.0, max_order_qty=5000,
                options_max_contracts_per_order=10,
                sleeve_momentum_enabled=False, sleeve_putwrite_enabled=False, sleeve_vaa_enabled=False)
    base.update(kw)
    return dataclasses.replace(load_settings(), **base)


@pytest.fixture
def env(tmp_path):
    at("2026-12-01", "11:00")
    db = DB(tmp_path / "t.db", "mock", None, None)
    broker = FakeBroker()

    def build(**kw):
        eng = FakeEngine(make_settings(**kw), db, broker)
        return eng, SleeveManager(eng)
    yield SimpleNamespace(db=db, broker=broker, build=build)
    set_test_time(None)
    db.close()


def seed(db, sid, cash=500000.0, **kw):
    db.upsert_sleeve_state(sid, cash=cash, start_capital=500000.0, start_date="2026-12-01",
                           peak_equity=kw.pop("peak", 500000.0), paused=False, **kw)


def seed_targets(db, sid, tg, cash=500000.0, **kw):
    seed(db, sid, cash, meta={"targets": tg, "targets_month": MONTH, "passes": 0}, **kw)


def logs(db):
    return [r[0] for r in db.conn.execute("SELECT message FROM events").fetchall()]


# ---------------------------------------------------------------- (a) first tick
async def test_first_tick_creates_states(env):
    eng, mgr = env.build()
    await mgr.tick()
    assert mgr.last_error is None
    for sid in ("momentum", "putwrite", "vaa"):
        st = env.db.sleeve_state(sid)
        assert st["cash"] == 500000.0 and st["start_capital"] == 500000.0 and st["paused"] == 0
    assert env.db.sleeve_state("swing")["start_date"] == "2026-12-01"
    assert env.broker.placed == []


async def test_tick_noop_when_disabled(env):
    eng, mgr = env.build(sleeves_enabled=False)
    await mgr.tick()
    assert env.db.sleeve_states() == {}


# ---------------------------------------------------------------- (b)/(c) rebalance
async def test_sells_first_then_buys_after_settle(env):
    env.broker.prices = {"AAA": 100.0, "BBB": 50.0}
    env.broker.qty = {"AAA": 100}
    seed_targets(env.db, "momentum", {"BBB": 1.0})
    env.db.set_sleeve_holding("momentum", "AAA", "stock", 100, 100.0)
    eng, mgr = env.build(sleeve_momentum_enabled=True)

    await mgr.tick()
    assert mgr.last_error is None
    assert [(p["sym"], p["side"], p["qty"]) for p in env.broker.placed] == [("AAA", "SELL", 100)]
    assert env.broker.placed[0]["session"] == "CORE"
    assert env.db.sleeve_holdings("momentum") == []          # removed when sent

    await mgr.tick()                                         # sell still working: nothing new
    assert len(env.broker.placed) == 1

    env.broker.fill(env.broker.placed[0]["coid"], 100, 99.0)
    await mgr.tick()                                         # sell settles, cash credited, buys go out
    assert env.db.sleeve_state("momentum")["cash"] == pytest.approx(500000 + 9900)
    buys = env.broker.placed[1:]
    assert buys and all(p["sym"] == "BBB" and p["side"] == "BUY" for p in buys)


async def test_buy_fill_adds_holding_and_reduces_cash(env):
    env.broker.prices = {"BBB": 50.0}
    seed_targets(env.db, "momentum", {"BBB": 1.0})
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    await mgr.tick()
    first = env.broker.placed[0]
    assert first["side"] == "BUY"
    env.broker.fill(first["coid"], first["qty"], 49.5)
    await mgr.sync_orders()
    h = env.db.sleeve_holdings("momentum")
    assert len(h) == 1 and h[0]["symbol"] == "BBB" and h[0]["qty"] == first["qty"]
    assert h[0]["avg_cost"] == pytest.approx(49.5)
    assert env.db.sleeve_state("momentum")["cash"] == pytest.approx(500000 - first["qty"] * 49.5)
    assert env.db.sleeve_reserved() == {"BBB": float(first["qty"])}


async def test_sell_cancelled_restores_unfilled_rest(env):
    env.broker.prices = {"AAA": 100.0, "BBB": 50.0}
    env.broker.qty = {"AAA": 100}
    seed_targets(env.db, "momentum", {"BBB": 1.0})
    env.db.set_sleeve_holding("momentum", "AAA", "stock", 100, 100.0)
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    await mgr.tick()
    assert env.db.sleeve_holdings("momentum") == []
    env.broker.fill(env.broker.placed[0]["coid"], 60, 100.0, status="CANCELLED")
    await mgr.sync_orders()
    h = env.db.sleeve_holdings("momentum")
    assert len(h) == 1 and h[0]["symbol"] == "AAA" and h[0]["qty"] == 40
    assert env.db.sleeve_state("momentum")["cash"] == pytest.approx(500000 + 6000)
    assert env.db.sleeve_orders("momentum", 1)[0]["status"] == "cancelled"


# ---------------------------------------------------------------- (d) chunking
async def test_chunking_respects_caps(env):
    env.broker.prices = {"BBB": 50.0}
    seed_targets(env.db, "momentum", {"BBB": 1.0})
    eng, mgr = env.build(sleeve_momentum_enabled=True, max_order_usd=10000.0, max_order_qty=150)
    await mgr.tick()
    assert env.broker.placed
    for p in env.broker.placed:
        assert p["qty"] <= 150 and p["qty"] * p["px"] <= 10000.0
    total = sum(p["qty"] for p in env.broker.placed)
    assert total * 50.0 <= 500000 * 0.995


async def test_share_price_above_cap_warns_and_sends_nothing(env):
    env.broker.prices = {"BIG": 60000.0}
    seed_targets(env.db, "momentum", {"BIG": 1.0})
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    await mgr.tick()
    assert mgr.last_error is None
    assert env.broker.placed == []
    assert any("MAX_ORDER_USD" in m for m in logs(env.db))


# ---------------------------------------------------------------- (e) put-write
CONTRACTS = ("QQQ261231P00380000 QQQ261231P00385000 QQQ261231P00390000 QQQ261231C00385000 "
             "QQQ261201P00385000 QQQ270630P00385000")


def pw_chain(env):
    env.broker.prices = {"QQQ": 400.0}
    env.broker.contracts_text = CONTRACTS
    env.broker.snaps = {
        "QQQ261231P00380000": {"delta": -0.25, "bid": 4.0, "ask": 4.2},
        "QQQ261231P00385000": {"delta": -0.30, "bid": 5.0, "ask": 5.2},
        "QQQ261231P00390000": {"delta": -0.36, "bid": 6.0, "ask": 6.2},
    }


async def test_putwrite_opens_in_pieces_and_fill_books_short(env):
    pw_chain(env)
    seed(env.db, "putwrite")
    eng, mgr = env.build(sleeve_putwrite_enabled=True)
    await mgr.tick()
    assert mgr.last_error is None
    o = env.broker.opt_placed
    assert [p["qty"] for p in o] == [10, 2]                    # floor(500000/38500)=12
    assert all(p["sym"] == "QQQ261231P00385000" and p["side"] == "SELL" and p["intent"] == "SELL_TO_OPEN"
               and p["coid"].startswith("sp") and len(p["coid"]) == 32 for p in o)
    assert env.db.sleeve_option_symbols() == {"QQQ261231P00385000"}
    for p in o:
        env.broker.fill(p["coid"], p["qty"], 5.05)
    await mgr.sync_orders()
    h = env.db.sleeve_holdings("putwrite")
    assert len(h) == 1 and h[0]["kind"] == "option" and h[0]["qty"] == -12
    assert env.db.sleeve_state("putwrite")["cash"] == pytest.approx(500000 + 12 * 100 * 5.05 - 12 * 0.65)  # minus est. $0.65/contract fee
    assert env.db.sleeve_reserved() == {}                      # options never reserve shares


async def test_putwrite_itm_on_expiry_day_after_1530_buys_back(env):
    pw_chain(env)
    at("2026-12-01", "15:35")
    seed(env.db, "putwrite")
    env.db.set_sleeve_holding("putwrite", "QQQ261201P00385000", "option", -2, 5.0)
    env.broker.prices = {"QQQ": 380.0}
    env.broker.options_text = "Symbol: QQQ261201P00385000 Qty: -2 Mark: 6.0 Type: OPTION"
    env.broker.snaps = {"QQQ261201P00385000": {"bid": 5.8, "ask": 6.0}}
    eng, mgr = env.build(sleeve_putwrite_enabled=True)
    await mgr.tick()
    assert mgr.last_error is None
    o = env.broker.opt_placed
    assert len(o) == 1 and o[0]["intent"] == "BUY_TO_CLOSE" and o[0]["side"] == "BUY" and o[0]["qty"] == 2
    assert o[0]["allow_when_killed"] is True


async def test_putwrite_otm_before_1530_does_nothing(env):
    pw_chain(env)
    at("2026-12-01", "15:00")
    seed(env.db, "putwrite")
    env.db.set_sleeve_holding("putwrite", "QQQ261201P00385000", "option", -2, 5.0)
    env.broker.options_text = "Symbol: QQQ261201P00385000 Qty: -2 Mark: 6.0 Type: OPTION"
    env.broker.prices = {"QQQ": 380.0}
    eng, mgr = env.build(sleeve_putwrite_enabled=True)
    await mgr.tick()
    assert env.broker.opt_placed == []


async def test_putwrite_expired_worthless(env):
    at("2026-12-02", "11:00")
    env.broker.prices = {"QQQ": 400.0}
    seed(env.db, "putwrite", cash=510000.0)
    env.db.set_sleeve_holding("putwrite", "QQQ261201P00385000", "option", -2, 5.0)
    eng, mgr = env.build(sleeve_putwrite_enabled=True)
    await mgr.tick()
    assert mgr.last_error is None
    assert env.db.sleeve_holdings("putwrite") == []
    assert env.db.sleeve_state("putwrite")["cash"] == 510000.0
    assert env.broker.opt_placed == [] and env.broker.placed == []


async def test_putwrite_assigned_books_stock_then_sells(env):
    at("2026-12-02", "11:00")
    env.broker.prices = {"QQQ": 380.0}
    env.broker.qty = {"QQQ": 200}
    seed(env.db, "putwrite", cash=500000.0)
    env.db.set_sleeve_holding("putwrite", "QQQ261201P00385000", "option", -2, 5.0)
    eng, mgr = env.build(sleeve_putwrite_enabled=True)
    await mgr.tick()
    assert mgr.last_error is None
    h = env.db.sleeve_holdings("putwrite")
    assert [(x["symbol"], x["kind"], x["qty"], x["avg_cost"]) for x in h] == [("QQQ", "stock", 200.0, 385.0)]
    assert env.db.sleeve_state("putwrite")["cash"] == pytest.approx(500000 - 200 * 385)
    assert env.db.sleeve_reserved() == {"QQQ": 200.0}
    assert env.broker.placed == []
    await mgr.tick()                                           # next step sells the shares
    assert sum(p["qty"] for p in env.broker.placed) == 200
    assert all(p["side"] == "SELL" and p["sym"] == "QQQ" for p in env.broker.placed)
    assert env.db.sleeve_holdings("putwrite") == []


# ---------------------------------------------------------------- (f) drawdown pause
async def test_drawdown_pause_at_limit_and_no_openings(env):
    env.broker.prices = {"BBB": 50.0}
    seed_targets(env.db, "momentum", {"BBB": 1.0}, cash=375000.0)     # exactly -25%
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    from app.sleeves.manager import DD_CONFIRM_TICKS
    for i in range(DD_CONFIRM_TICKS):
        await mgr._drawdown_checks(await _values(mgr))
        paused = env.db.sleeve_state("momentum")["paused"]
        assert paused == (1 if i == DD_CONFIRM_TICKS - 1 else 0)   # one bad reading never pauses
    await mgr.tick()
    st = env.db.sleeve_state("momentum")
    assert st["paused"] == 1 and "drawdown" in st["pause_reason"]
    assert env.broker.placed == []


async def _values(mgr):
    return mgr._value(await mgr._snapshot())


async def test_peak_ignores_one_minute_spike(env):
    seed_targets(env.db, "momentum", {"BBB": 1.0}, cash=500000.0)
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    await mgr._drawdown_checks({"momentum": {"equity": 500000.0}})
    await mgr._drawdown_checks({"momentum": {"equity": 900000.0}})      # glitch: +80% in a minute
    assert env.db.sleeve_state("momentum")["peak_equity"] == 500000.0
    await mgr._drawdown_checks({"momentum": {"equity": 510000.0}})
    assert env.db.sleeve_state("momentum")["peak_equity"] == 510000.0


async def test_no_pause_above_limit(env):
    env.broker.prices = {"BBB": 50.0}
    seed_targets(env.db, "momentum", {"BBB": 1.0}, cash=380000.0)     # -24%
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    await mgr.tick()
    assert env.db.sleeve_state("momentum")["paused"] == 0
    assert env.broker.placed


async def test_paused_putwrite_sends_no_opening(env):
    pw_chain(env)
    seed(env.db, "putwrite", cash=300000.0)
    env.db.upsert_sleeve_state("putwrite", paused=True, pause_reason="x")
    eng, mgr = env.build(sleeve_putwrite_enabled=True)
    await mgr.tick()
    assert env.broker.opt_placed == []


# ---------------------------------------------------------------- (g) kill switch
async def test_kill_switch_blocks_openings(env):
    pw_chain(env)
    env.broker.prices["BBB"] = 50.0
    seed_targets(env.db, "momentum", {"BBB": 1.0})
    seed(env.db, "putwrite")
    eng, mgr = env.build(sleeve_momentum_enabled=True, sleeve_putwrite_enabled=True)
    eng.kill_switch = True
    await mgr.tick()
    assert env.broker.placed == [] and env.broker.opt_placed == []


async def test_kill_switch_freezes_rebalance(env):
    """By design (docs/SLEEVES_ARCHITECTURE.md section 2) the kill switch freezes rebalancing:
    neither the sell nor the buy leg is sent."""
    env.broker.prices = {"AAA": 100.0, "BBB": 50.0}
    env.broker.qty = {"AAA": 100}
    seed_targets(env.db, "momentum", {"BBB": 1.0})
    env.db.set_sleeve_holding("momentum", "AAA", "stock", 100, 100.0)
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    eng.kill_switch = True
    await mgr.tick()
    assert env.broker.placed == []


# ---------------------------------------------------------------- (h)/(i) views and controls
async def test_summary_disabled(env):
    eng, mgr = env.build(sleeves_enabled=False)
    out = await mgr.summary()
    assert out["enabled"] is False
    assert [s["id"] for s in out["sleeves"]] == ["swing", "momentum", "putwrite", "vaa"]
    assert all(s["status"] == "disabled" for s in out["sleeves"])


async def test_summary_enabled_waiting_and_detail(env):
    seed(env.db, "momentum")
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    out = await mgr.summary()
    m = next(s for s in out["sleeves"] if s["id"] == "momentum")
    assert out["enabled"] is True and m["status"] == "waiting" and m["start_capital"] == 500000.0
    d = await mgr.detail("momentum")
    assert d["holdings"] == [] and d["orders"] == [] and d["targets"] == []


async def test_rebalance_now_swing_raises_and_others_reset(env):
    seed_targets(env.db, "momentum", {"BBB": 1.0})
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    with pytest.raises(ValueError):
        await mgr.rebalance_now("swing")
    await mgr.rebalance_now("momentum")
    st = env.db.sleeve_state("momentum")
    assert st["last_rebalance"] is None and "targets" not in (st["meta_json"] or "")


async def test_set_paused_swing_stops_scheduler(env):
    eng, mgr = env.build()
    await mgr.set_paused("swing", True, "r")
    assert eng.sched_calls == ["stop"] and eng.scheduler_stopped_by_user is True
    await mgr.set_paused("swing", False, "r")
    assert eng.sched_calls == ["stop", "start"]
    await mgr.set_paused("momentum", True, "r")
    assert env.db.sleeve_state("momentum")["paused"] == 1


async def test_capital_shortfall_blocks_trading(env):
    """A $25k account cannot fund four $500k sleeves: nothing trades and the API says why."""
    seed_targets(env.db, "momentum", {"BBB": 1.0})
    env.broker.prices = {"BBB": 50.0}
    eng, mgr = env.build(sleeve_momentum_enabled=True)

    class SmallPortfolio:
        _cache = {"positions": []}

        async def snapshot(self, *a, **k):
            return {"balance": {"net_liq": 25_000.0}}
    eng.portfolio = SmallPortfolio()
    await mgr.tick()
    assert env.broker.placed == []
    out = await mgr.summary()
    assert all(x["status"] == "disabled" for x in out["sleeves"])
    assert "NOT running" in (out["last_error"] or "")


async def test_others_day_pnl_feeds_swing_daily_loss(env):
    """The swing daily-loss check sees account P&L minus the other sleeves' day P&L."""
    from datetime import timedelta
    from app.market import now_et
    seed_targets(env.db, "momentum", {"BBB": 1.0}, cash=500000.0)
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    y = (now_et().date() - timedelta(days=1)).isoformat()
    env.db.record_sleeve_equity(y, "momentum", 520000.0)
    mgr.last_values = {"momentum": {"equity": 500000.0}}
    assert mgr.others_day_pnl() == -20000.0
    assert env.broker.__dict__["sleeve_day_pnl"]() == -20000.0


# ---------------------------------------------------------------- SPY line + start baseline
async def test_tick_records_spy_when_not_held(env):
    eng, mgr = env.build()

    async def last_price(sym):
        return 600.0 if sym == "SPY" else None
    env.broker.last_price = last_price                    # SPY absent from positions
    await mgr.tick()
    rows = env.db.sleeve_equity_series(30)
    assert rows and rows[-1].get("spy") == 600.0


async def test_equity_series_starts_from_common_baseline(env):
    eng, mgr = env.build(sleeve_momentum_enabled=True, sleeve_vaa_enabled=True)
    seed(env.db, "momentum")
    seed(env.db, "vaa")
    env.db.upsert_sleeve_state("swing", start_date="2026-12-01", paused=False)
    env.db.upsert_benchmark_daily("2026-11-27", net_liq=1_600_000.0, spy_close=590.0)
    env.db.upsert_benchmark_daily("2026-11-30", net_liq=1_500_000.0, spy_close=600.0)
    env.db.record_sleeve_equity("2026-11-28", "momentum", 1.0)     # stray row before the start
    env.db.record_sleeve_equity("2026-12-01", "momentum", 510000.0)
    env.db.record_sleeve_equity("2026-12-01", "spy", 606.0)
    out = mgr.equity_series(30)
    assert out["start_date"] == "2026-12-01"
    base, day1 = out["series"]
    assert base == {"day": "2026-11-30", "baseline": True, "momentum": 500000.0, "vaa": 500000.0,
                    "swing": 500000.0, "total": 1_500_000.0, "spy": 600.0}
    assert day1["day"] == "2026-12-01" and day1["momentum"] == 510000.0


async def test_equity_series_baseline_without_benchmark_close(env):
    eng, mgr = env.build(sleeve_momentum_enabled=True)
    seed(env.db, "momentum")
    env.db.upsert_sleeve_state("swing", start_date="2026-12-01", paused=False)
    base = mgr.equity_series(30)["series"][0]
    assert base == {"day": "2026-11-30", "baseline": True, "momentum": 500000.0}


async def test_equity_series_without_sleeves(env):
    eng, mgr = env.build()
    assert mgr.equity_series(30) == {"series": [], "start_date": None}
