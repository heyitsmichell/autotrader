"""Options auto-entry: contract selection, skips, proposal fields, exits, daily cap."""

import dataclasses
from datetime import datetime, timedelta

import pytest

from app.engine.option_entries import entry_limit_price, option_tick
from app.engine.option_exits import evaluate_exit
from app.engine.options_flow import OptionsFlowMixin
from app.market import ET, now_et, set_test_time

TODAY = datetime(2026, 11, 20, 10, 0, tzinfo=ET)


def occ(sym, exp, strike, cp="C"):
    return f"{sym}{exp.strftime('%y%m%d')}{cp}{int(strike * 1000):08d}"


class FakeBroker:
    """Chain for one underlying at spot 100; deltas fall as the strike rises."""

    def __init__(self, ask_scale=1.0, volume=500, oi=5000, spread=0.04):
        self.spot, self.ask_scale, self.volume, self.oi, self.spread = 100.0, ask_scale, volume, oi, spread
        self.calls = {"contracts": 0, "snap": 0}
        self.exps = [TODAY.date() + timedelta(days=d) for d in (10, 29, 45)]

    async def last_price(self, sym):
        return self.spot

    async def option_contracts(self, syms, **kw):
        self.calls["contracts"] += 1
        sym = syms[0]
        return "\n".join(occ(sym, e, k) + f" Type: CALL Strike: {k}" for e in self.exps
                         for k in range(70, 131, 5))

    async def parsed_option_snapshots(self, symbols):
        self.calls["snap"] += 1
        out = {}
        for s in symbols:
            k = int(s[-8:]) / 1000
            delta = max(0.02, min(0.98, 0.5 + (self.spot - k) / 40))
            mid = max(0.2, (self.spot - k) + 4.0) * self.ask_scale
            out[s] = {"bid": round(mid * (1 - self.spread / 2), 2), "ask": round(mid * (1 + self.spread / 2), 2),
                      "delta": delta, "iv": 0.30, "volume": self.volume, "open_interest": self.oi,
                      "as_of": 1.0}
        return out


@pytest.fixture
def eng(test_engine, monkeypatch):
    s = dataclasses.replace(
        test_engine.s, options_enabled=True, options_auto_entry=True,
        options_allowed_underlyings=["AAPL", "MSFT"], options_entry_earnings_days=7,
        options_entry_max_premium_usd=3000.0,
        # keep the broker-guard caps out of the way so these tests exercise the entry budget
        max_order_usd=100000.0, options_max_notional_usd=100000.0)
    test_engine.s = s
    test_engine.broker = FakeBroker()

    async def no_held():
        return []
    monkeypatch.setattr(test_engine.option_exits, "held_options", no_held)

    from app import eligibility

    async def no_earnings(self, symbol):
        return []
    monkeypatch.setattr(eligibility.EligibilityChecker, "_get_earnings_dates", no_earnings)
    test_engine.captured = []

    async def spy(run_id, p, sig_override=None):
        test_engine.captured.append(p)
        return f"ACCEPTED option proposal #{len(test_engine.captured)}"
    test_engine._on_proposal = spy
    return test_engine


def with_settings(e, **kw):
    e.s = dataclasses.replace(e.s, **kw)


def sig(sym="AAPL", **kw):
    return {"symbol": sym, "consensus": "long", "close": 100.0, "atr": 2.0, "ta_score": 0.36,
            "ml_p": 0.6, **kw}


def log_text(e):
    return " | ".join(r[0] for r in e.db.conn.execute("SELECT message FROM events").fetchall())


async def test_off_by_default(test_engine):
    assert test_engine.s.options_auto_entry is False
    assert await test_engine.propose_option_entries(1, [sig()]) == 0


async def test_selects_delta_dte_and_builds_proposal(eng):
    n = await eng.propose_option_entries(1, [sig()])
    assert n == 1, log_text(eng)
    p = eng.captured[0]
    assert p["side"] == "BUY" and p["position_intent"] == "BUY_TO_OPEN"
    assert p["option_type"] == "CALL" and p["option_strategy"] == "SINGLE"
    assert p["expiration_date"] == (TODAY.date() + timedelta(days=29)).isoformat()   # nearest 30 DTE
    assert p["strike_price"] == 90.0                    # delta 0.75, nearest 0.70 of the quoted strikes
    assert p["underlying"] == "AAPL" and p["underlying_entry_close"] == 100.0
    assert p["underlying_stop"] and p["underlying_stop"] < 100.0
    assert p["quantity"] >= 1
    assert "Call on AAPL" in p["rationale"] and "29 DTE" in p["rationale"]
    sel = p["selection"]
    assert {"delta", "dte", "iv", "iv_rank", "spread_pct", "underlying_close", "ta_score",
            "ml_p"} <= set(sel)


async def test_delta_closest_to_target(eng):
    await eng.propose_option_entries(1, [sig()])
    d = eng.captured[0]["selection"]["delta"]
    assert abs(d - 0.70) <= 0.13          # strikes are $5 apart = 0.125 delta steps


async def test_quantity_and_limit_price(eng):
    await eng.propose_option_entries(1, [sig()])
    p = eng.captured[0]
    ask = p["selection"]["ask"]
    assert p["quantity"] == int(3000 // (ask * 100))
    assert p["limit_price"] <= ask
    assert round(p["limit_price"] / option_tick(p["limit_price"])) * option_tick(p["limit_price"]) \
        == pytest.approx(p["limit_price"], abs=1e-6)


def test_limit_price_helpers():
    assert option_tick(2.99) == 0.01 and option_tick(3.0) == 0.05
    assert entry_limit_price(1.00, 1.10) in (1.07, 1.08)      # mid 1.05 + half to ask = 1.075 -> 1.07/1.08
    assert entry_limit_price(4.00, 4.40) == 4.30      # 4.30 on the 0.05 grid
    assert entry_limit_price(4.00, 4.02) <= 4.02


async def test_unaffordable_skips_with_plain_message(eng):
    eng.broker.ask_scale = 3.0
    with_settings(eng, options_entry_max_premium_usd=100.0)
    assert await eng.propose_option_entries(7, [sig()]) == 0
    assert eng.captured == []
    assert "one contract costs" in log_text(eng)
    assert "budget $100" in log_text(eng)


async def test_illiquid_contracts_skipped(eng):
    eng.broker.volume = 1
    assert await eng.propose_option_entries(1, [sig()]) == 0
    assert "liquidity" in log_text(eng)
    eng.broker.volume, eng.broker.spread = 500, 0.5
    assert await eng.propose_option_entries(2, [sig()]) == 0


async def test_not_core_session_skips(eng):
    set_test_time(datetime(2026, 11, 20, 18, 0, tzinfo=ET))
    assert await eng.propose_option_entries(1, [sig()]) == 0
    assert eng.broker.calls["contracts"] == 0


@pytest.mark.parametrize("hh,mm", [(9, 40), (15, 50)])
async def test_open_and_close_minutes_skipped(eng, hh, mm):
    set_test_time(datetime(2026, 11, 20, hh, mm, tzinfo=ET))
    assert await eng.propose_option_entries(1, [sig()]) == 0
    assert "minutes of the open or close" in log_text(eng)
    with_settings(eng, options_entry_skip_open_close_minutes=0)
    assert await eng.propose_option_entries(2, [sig()]) == 1


async def test_duplicate_underlying_skipped(eng, monkeypatch):
    from app.engine.option_exits import HeldOption
    sym = occ("AAPL", TODAY.date() + timedelta(days=30), 80)

    async def held():
        return [HeldOption(sym, 1.0, 5.0, 5.0)]
    monkeypatch.setattr(eng.option_exits, "held_options", held)
    assert await eng.propose_option_entries(1, [sig("AAPL"), sig("MSFT")]) == 1
    assert [p["underlying"] for p in eng.captured] == ["MSFT"]
    assert "AAPL call skipped" in log_text(eng)


async def test_max_open_limit_and_no_limit(eng, monkeypatch):
    from app.engine.option_exits import HeldOption
    held_rows = [HeldOption(occ("QQQ", TODAY.date() + timedelta(days=30), 80), 1.0, 5.0, 5.0)
                 for _ in range(1)]

    async def held():
        return held_rows
    monkeypatch.setattr(eng.option_exits, "held_options", held)
    with_settings(eng, options_entry_max_open=1)
    assert await eng.propose_option_entries(1, [sig("AAPL")]) == 0
    with_settings(eng, options_entry_max_open=0)         # 0 = no limit
    assert await eng.propose_option_entries(2, [sig("AAPL"), sig("MSFT")]) == 2


async def test_max_per_day_limit(eng):
    with_settings(eng, options_entry_max_per_day=1)
    assert await eng.propose_option_entries(1, [sig("AAPL"), sig("MSFT")]) == 1


async def test_earnings_within_window_skips(eng, monkeypatch):
    from app import eligibility
    soon = (TODAY.date() + timedelta(days=3)).isoformat()

    async def earn(self, symbol):
        return [soon]
    monkeypatch.setattr(eligibility.EligibilityChecker, "_get_earnings_dates", earn)
    assert await eng.propose_option_entries(1, [sig()]) == 0
    assert "arnings" in log_text(eng)


async def test_kill_switch_and_non_long_and_not_allowed(eng):
    assert await eng.propose_option_entries(1, [sig("NVDA"), sig("AAPL", consensus="none")]) == 0
    eng.kill_switch = True
    assert await eng.propose_option_entries(2, [sig()]) == 0


async def test_routes_through_on_proposal_no_candidate_cap(eng):
    with_settings(eng, options_allowed_underlyings=[f"S{c}" for c in "ABCDEFGH"])
    sigs = [sig(f"S{c}") for c in "ABCDEFGH"]
    # the fake chain returns the requested underlying's symbols
    n = await eng.propose_option_entries(1, sigs)
    assert n == 8 and len(eng.captured) == 8            # no per-run candidate cap
    assert eng.broker.calls["contracts"] == 8 and eng.broker.calls["snap"] == 8


async def test_deterministic_cycle_calls_option_entries(eng, monkeypatch):
    seen = []

    async def fake(run_id, sigs):
        seen.append(run_id)
        return 1
    monkeypatch.setattr(eng, "propose_option_entries", fake)

    async def positions():
        return {}
    eng.broker.positions = positions
    n = await eng._generate_proposals_deterministic(5, [])
    assert seen == [5] and n == 1
    with_settings(eng, options_auto_entry=False)
    seen.clear()
    await eng._generate_proposals_deterministic(6, [])
    assert seen == []


def test_proposal_row_keeps_underlying_stop(eng):
    import asyncio
    p = {"symbol": occ("AAPL", TODAY.date() + timedelta(days=30), 80), "side": "BUY", "quantity": 1,
         "limit_price": 5.0, "option_type": "CALL", "strike_price": 80.0,
         "expiration_date": "2026-12-20", "position_intent": "BUY_TO_OPEN",
         "underlying": "AAPL", "underlying_stop": 96.5, "underlying_entry_close": 100.0,
         "selection": {"delta": 0.7, "ml_p": 0.6}}
    gate = {"checks": [], "sl": None, "tp": None, "ok": True}
    run_id = eng.db.start_run("test") if hasattr(eng.db, "start_run") else 1
    res = asyncio.get_event_loop().run_until_complete(
        OptionsFlowMixin._create_option_proposal(eng, None, p, gate, None, run_id))
    assert res.startswith("ACCEPTED")
    row = eng.db.proposals(1)[0]
    assert row["sl"] == 96.5
    assert "selection" in row["instrument_json"] and "underlying_stop" in row["instrument_json"]


# ---------------------------------------------------------------- exits (pure)
def exit_s(**kw):
    from app.config import load_settings
    return dataclasses.replace(load_settings(), options_exit_underlying_stop=True,
                               exit_max_hold_days=20, options_exit_take_profit_pct=0.0, **kw)


def test_underlying_stop_trigger():
    s = exit_s()
    now = TODAY
    hit = evaluate_exit(s, entry=5.0, mark=4.0, expiration="2026-12-31", now=now,
                        underlying_px=95.0, underlying_stop=96.0)
    assert hit and hit[0] == "underlying_stop"
    assert evaluate_exit(s, entry=5.0, mark=4.0, expiration="2026-12-31", now=now,
                         underlying_px=97.0, underlying_stop=96.0) is None
    off = dataclasses.replace(s, options_exit_underlying_stop=False)
    assert evaluate_exit(off, entry=5.0, mark=4.0, expiration="2026-12-31", now=now,
                         underlying_px=90.0, underlying_stop=96.0) is None


def test_underlying_time_trigger_and_priority():
    s = exit_s()
    hit = evaluate_exit(s, entry=5.0, mark=4.0, expiration="2026-12-31", now=TODAY,
                        held_trading_days=20)
    assert hit and hit[0] == "underlying_time"
    assert evaluate_exit(s, entry=5.0, mark=4.0, expiration="2026-12-31", now=TODAY,
                         held_trading_days=19) is None
    off = dataclasses.replace(s, exit_max_hold_days=0)
    assert evaluate_exit(off, entry=5.0, mark=4.0, expiration="2026-12-31", now=TODAY,
                         held_trading_days=99) is None
    # DTE beats underlying_stop; underlying_stop beats the premium stop
    assert evaluate_exit(s, entry=5.0, mark=1.0, expiration="2026-11-21", now=TODAY,
                         underlying_px=90.0, underlying_stop=96.0)[0] == "dte"
    assert evaluate_exit(s, entry=5.0, mark=1.0, expiration="2026-12-31", now=TODAY,
                         underlying_px=90.0, underlying_stop=96.0)[0] == "underlying_stop"


# ---------------------------------------------------------------- daily cap
async def test_equity_daily_cap_ignores_option_orders(test_engine):
    db = test_engine.db
    run = 1
    for i in range(3):
        sym = occ("AAPL", TODAY.date() + timedelta(days=30), 80 + i)
        db.add_proposal(run, {"symbol": sym, "side": "BUY", "quantity": 1, "limit_price": 5.0},
                        "placed", "", 5.0, None, asset_class="option", option_type="CALL",
                        strike_price=80 + i, expiration_date="2026-12-20",
                        position_intent="BUY_TO_OPEN")
    assert db.count_placed_today() == 3
    assert db.count_placed_today("equity") == 0
    assert db.count_placed_today("option") == 3
    db.add_proposal(run, {"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": 100.0},
                    "placed", "", 100.0, None)
    assert db.count_placed_today("equity") == 1


def test_settings_defaults_and_env_example():
    from app.config import load_settings
    s = load_settings()
    assert s.options_auto_entry is False and s.options_entry_target_delta == 0.70
    assert s.options_entry_target_dte == 30 and s.options_entry_max_premium_usd == 500.0
    assert s.options_entry_max_open == 0 and s.options_exit_underlying_stop is True




async def test_sizing_respects_broker_order_cap(eng):
    """Regression (2026-11-25 mock smoke test): app/broker/guard.py caps option notional at
    MAX_ORDER_USD; sizing above it got every order refused and tripped the kill switch."""
    await eng.propose_option_entries(10, [sig()])           # learn the fake contract's ask
    one = eng.captured[0]["selection"]["ask"] * 100
    assert eng.captured[0]["quantity"] >= 2                  # the $3,000 budget buys several
    eng.captured.clear()
    eng.db.open_proposals = lambda: []                       # forget the first proposal
    with_settings(eng, max_order_usd=one * 1.5)              # broker cap affords exactly one
    await eng.propose_option_entries(11, [sig()])
    p = eng.captured[0]
    assert p["quantity"] == 1
    assert p["quantity"] * p["limit_price"] * 100 <= one * 1.5 + 1e-6
