"""Opus review batch 2: C8, C9, C11, C12, C14, C15, S18."""
import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from tests.test_option_entries import eng, with_settings, sig, occ, TODAY  # noqa: F401
from tests.test_fastloop import _loop, _bar
from app import fastloop, sizing, exits
from app.config import load_settings
from app.engine import option_entries
from app.engine.option_entries import entry_limit_price


async def test_c8_contracts_clamped_to_per_order_cap(eng):
    with_settings(eng, options_max_contracts_per_order=2)
    await eng.propose_option_entries(1, [sig()])
    assert eng.captured and eng.captured[0]["quantity"] == 2


async def test_c9_working_option_buys_count_toward_max_open(eng, monkeypatch):
    exp = TODAY.date() + timedelta(days=30)
    working = [occ(u, exp, 50) for u in ("AAA", "BBB", "CCC")]
    monkeypatch.setattr(option_entries, "working_buy_symbols", lambda db: working)
    with_settings(eng, options_entry_max_open=3)
    assert await eng.propose_option_entries(1, [sig("AAPL")]) == 0


def test_c11_fractional_floors_never_rounds_up():
    s = SimpleNamespace(fractional_enabled=True, fractional_decimals=2)
    assert sizing._floor_dec(1.239, 2) == 1.23
    assert sizing._floor_dec(0.29, 2) == 0.29
    s2 = load_settings()
    s2 = type(s2)(**{**s2.__dict__, "fractional_enabled": True, "fractional_decimals": 2,
                     "volatility_scaling_enabled": True, "volatility_scaling_max_factor": 0.5})
    # C12: factor < 1 must not flip the clamp (scale stays within [1,1])
    assert sizing.volatility_scaled_qty(s2, 10.0, 1.0, 100.0) == 10.0


def test_c14_no_float_dust():
    s = SimpleNamespace(fractional_enabled=True, fractional_whitelist=["AAA"], fractional_decimals=2)
    assert exits.sell_quantity(0.29, s, "AAA") == 0.29      # int(0.29*100)=28 before
    assert exits.sell_quantity(4.35, s, "AAA") == 4.35
    assert exits.partial_quantity(0.58, 0.5, s, "AAA") == 0.29


def test_c15_limit_never_below_bid():
    assert entry_limit_price(3.40, 3.45) >= 3.40
    assert entry_limit_price(3.40, 3.45) <= 3.45


def test_s18_ml_exit_runs_with_kill_switch_on(monkeypatch):
    attrs = {"market_is_open": lambda: True,
             "now_et": lambda: datetime(2026, 11, 20, 11, 0),
             "indicators": SimpleNamespace(features=lambda b, bench=None: {"close": b[-1]["close"]}, ml_features=lambda b, bench=None: {"close": b[-1]["close"]}),
             "ta": SimpleNamespace(analyze=lambda f, s: {
                 "close": f["close"], "atr": 2.0, "trend": "down", "strength": 0.5, "vol": "normal",
                 "ta_score": -0.4, "ta_components": []}, levels=fastloop.ta.levels)}
    for k, v in attrs.items():
        monkeypatch.setattr(fastloop, k, v)
    loop, calls = _loop(price=97.0)
    loop.s = loop.s.__class__(**{**loop.s.__dict__, "ml_enabled": True, "exit_ml_drop_enabled": True})

    async def held():
        return {"AAA": 5.0}
    loop.broker.positions = held
    loop.signals.ml.predict = lambda f: 0.01
    loop.engine.kill_switch = True
    fired = asyncio.run(loop.tick())
    assert len(fired) == 1 and calls and calls[0][0]["side"] == "SELL"
