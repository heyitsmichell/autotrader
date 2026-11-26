"""ML-first mode: the model ranks, sizes and (with a TA floor) selects entries; rules are the fallback."""

import dataclasses

import pytest

from app import consensus
from app.sizing import ml_size_factor


def _sig(**kw):
    base = {"id": None, "symbol": "X", "close": 100.0, "atr": 2.0, "trend": "up",
            "ta_score": 0.05, "sent_score": 0.3, "sent_n": 6, "ml_p": None, "features": {}}
    base.update(kw)
    return base


BUY = {"side": "BUY", "limit_price": 100.0}


def _s(test_settings, **kw):
    return dataclasses.replace(test_settings, **kw)


def test_without_a_model_the_rules_threshold_still_applies(test_settings):
    s = _s(test_settings, consensus_ta_min=0.15, ml_first=True)
    assert consensus.evaluate(BUY, _sig(ta_score=0.05, ml_p=None), s)["ok"] is False


def test_model_in_charge_lowers_ta_to_a_floor(test_settings):
    s = _s(test_settings, consensus_ta_min=0.15, ml_first=True, ml_first_ta_floor=0.0, ml_min_p=0.52)
    assert consensus.evaluate(BUY, _sig(ta_score=0.05, ml_p=0.65), s)["ok"] is True


def test_ml_first_still_needs_the_model_to_approve(test_settings):
    s = _s(test_settings, consensus_ta_min=0.15, ml_first=True, ml_first_ta_floor=0.0, ml_min_p=0.52)
    assert consensus.evaluate(BUY, _sig(ta_score=0.5, ml_p=0.40), s)["ok"] is False


def test_ml_first_keeps_the_ta_floor_and_downtrend_block(test_settings):
    s = _s(test_settings, consensus_ta_min=0.15, ml_first=True, ml_first_ta_floor=0.0, ml_min_p=0.52)
    # Below TA floor still blocked
    assert consensus.evaluate(BUY, _sig(ta_score=-0.2, ml_p=0.9), s)["ok"] is False
    # Downtrend blocked when ML prob is below override threshold (0.7)
    assert consensus.evaluate(BUY, _sig(ta_score=0.4, ml_p=0.65, trend="down"), s)["ok"] is False
    # Downtrend ALLOWED when ML prob exceeds override threshold (0.7)
    assert consensus.evaluate(BUY, _sig(ta_score=0.4, ml_p=0.9, trend="down"), s)["ok"] is True


def test_ml_first_off_restores_old_behaviour(test_settings):
    s = _s(test_settings, consensus_ta_min=0.15, ml_first=False, ml_min_p=0.52)
    assert consensus.evaluate(BUY, _sig(ta_score=0.05, ml_p=0.9), s)["ok"] is False


def test_floor_can_never_be_above_the_rules_threshold(test_settings):
    s = _s(test_settings, consensus_ta_min=0.15, ml_first=True, ml_first_ta_floor=0.6, ml_min_p=0.52)
    assert consensus.evaluate(BUY, _sig(ta_score=0.2, ml_p=0.7), s)["ok"] is True


def test_size_factor_scales_with_confidence(test_settings):
    s = _s(test_settings, ml_enabled=True, ml_size_scaling=True, ml_min_p=0.52,
           ml_size_full_p=0.62, ml_size_min_factor=0.4)
    assert ml_size_factor(s, None) == 1.0
    assert ml_size_factor(s, 0.52) == pytest.approx(0.4)
    assert ml_size_factor(s, 0.57) == pytest.approx(0.7)
    assert ml_size_factor(s, 0.80) == 1.0
    assert ml_size_factor(_s(s, ml_size_scaling=False), 0.52) == 1.0
    assert ml_size_factor(_s(s, ml_enabled=False), 0.52) == 1.0


def _bars(n=160, drift=0.004):
    import math
    from datetime import datetime, timedelta, timezone

    out = []
    for i in range(n):
        px = 100 * (1 + drift) ** i + 6 * math.sin(i * 0.3)
        out.append({"time": datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                    "open": px, "high": px * 1.012, "low": px * 0.988, "close": px, "volume": 1e6})
    return out


def test_dataset_is_universe_wide_and_ledger_overrides(test_settings):
    pytest.importorskip("numpy")
    from app.ml import build_dataset

    base = build_dataset({"AAA": _bars(), "BBB": _bars(drift=0.001)}, settings=test_settings)
    assert base and all(r["source"] == "bars" for r in base)
    assert {r["symbol"] for r in base} == {"AAA", "BBB"}

    target = base[len(base) // 2]

    class FakeDB:
        def closed_trades(self, limit=5000):
            return [{"symbol": target["symbol"], "exit_reason": "stop" if target["y"] == 1.0 else "target",
                     "entry_features": dict(zip(__import__("app.ml", fromlist=["FEATURES"]).FEATURES, target["x"])),
                     "entry_fill_time": target["date"] + "T14:30:00+00:00", "realized_pnl_pct": -2.0,
                     "entry_ta_score": 0.3, "hold_seconds": 3600}]

    merged = build_dataset({"AAA": _bars(), "BBB": _bars(drift=0.001)}, db=FakeDB(), settings=test_settings)
    hit = [r for r in merged if r["symbol"] == target["symbol"] and r["date"] == target["date"]]
    assert len(hit) == 1 and hit[0]["source"] == "ledger"
    assert hit[0]["y"] == (0.0 if target["y"] == 1.0 else 1.0)
    assert len(merged) == len(base)


class _FakeEngine:
    """Just enough of Engine for _generate_proposals_deterministic."""

    def __init__(self, s):
        from unittest.mock import AsyncMock, MagicMock
        self.s, self.research = s, None
        self.broker = MagicMock()
        self.broker.positions = AsyncMock(return_value={})
        self.broker.last_price = AsyncMock(return_value=100.0)
        self.db = MagicMock()
        self.db.pending_proposals.return_value = []
        self.proposed: list[dict] = []

        async def _on_proposal(run_id, p):
            self.proposed.append(p)
            return "ACCEPTED"
        self._on_proposal = _on_proposal


async def _run(engine, sigs):
    from app.engine import Engine
    return await Engine._generate_proposals_deterministic(engine, 1, sigs)


def _long(sym, p):
    return {"symbol": sym, "consensus": "long", "ml_p": p, "ta_score": 0.3, "sent_score": 0.1}


async def test_ml_first_buys_best_scores_first_and_caps(test_settings):
    s = _s(test_settings, ml_enabled=True, ml_first=True, ml_first_top_n=2,
           watchlist=["A", "B", "C", "D"])
    e = _FakeEngine(s)
    await _run(e, [_long("A", 0.55), _long("B", 0.71), _long("C", 0.63), _long("D", 0.58)])
    assert [p["symbol"] for p in e.proposed] == ["B", "C"]
    assert e.proposed[0]["confidence"] == 0.71


async def test_without_model_scores_order_and_cap_are_unchanged(test_settings):
    s = _s(test_settings, ml_enabled=True, ml_first=True, ml_first_top_n=1,
           watchlist=["A", "B", "C"])
    e = _FakeEngine(s)
    await _run(e, [_long("A", None), _long("B", None), _long("C", None)])
    assert [p["symbol"] for p in e.proposed] == ["A", "B", "C"]
    assert all(p["confidence"] == 0.7 for p in e.proposed)


async def test_promoting_a_challenger_makes_it_the_champion(test_settings, test_db):
    from app.ml import MLFilter

    ml = MLFilter(test_settings, test_db)
    assert test_db.champion_model() is None
    mid = test_db.add_model(status="challenger", path="x.pt", n_samples=10, n_symbols=2, metrics={}, params={})
    # M4: an unloadable checkpoint is refused and the DB is left unchanged
    with pytest.raises(ValueError):
        ml.promote_challenger(mid)
    assert test_db.champion_model() is None
    # a checkpoint that loads is promoted
    ml._load_checkpoint = lambda row: (None, None, None)
    out = ml.promote_challenger(mid)
    assert out["status"] == "champion"
    assert test_db.champion_model()["id"] == mid
    assert test_db.challenger_model() is None


def test_round_trip_cost_counts_both_sides_and_commission(test_settings):
    from app.backtest.costs import CostModel, round_trip_cost_pct

    m = CostModel(commission_per_order=1.0, slippage_bps=5.0, atr_slippage_mult=0.0,
                  impact_bps=5.0, max_impact_bps=100.0)
    # 10 bps slippage each way on $100 + $1 commission each way on 10 shares
    assert round_trip_cost_pct(m, 100.0, atr=None, notional=1000.0) == pytest.approx(0.2 + 0.2)
    zero = CostModel(commission_per_order=0.0, slippage_bps=0.0, atr_slippage_mult=0.0,
                     impact_bps=0.0, max_impact_bps=0.0)
    assert round_trip_cost_pct(zero, 100.0, atr=2.0, notional=1000.0) == 0.0


def test_cost_edge_check_blocks_a_target_that_does_not_cover_costs(test_settings):
    good = _s(test_settings, cost_edge_mult=3.0, consensus_ta_min=0.0)
    ok = consensus.evaluate(BUY, _sig(ta_score=0.6), good)
    assert any(c["name"] == "Cost edge" and c["ok"] for c in ok["checks"])

    # Huge costs: the same trade can no longer clear 3x the round-trip cost
    costly = _s(good, backtest_slippage_bps=200.0)
    blocked = consensus.evaluate(BUY, _sig(ta_score=0.6), costly)
    assert blocked["ok"] is False
    assert any(c["name"] == "Cost edge" and not c["ok"] for c in blocked["checks"])

    off = _s(costly, cost_edge_mult=0.0)
    assert not any(c["name"] == "Cost edge" for c in consensus.evaluate(BUY, _sig(ta_score=0.6), off)["checks"])


def test_ml_dataset_returns_are_net_of_costs(test_settings):
    from app.ml import build_dataset

    cheap = _s(test_settings, backtest_commission_per_order=0.0, backtest_slippage_bps=0.0,
               backtest_impact_bps=0.0, backtest_atr_slippage_mult=0.0)
    costly = _s(cheap, backtest_slippage_bps=50.0)
    bars = {"AAA": _bars()}
    a, b = build_dataset(bars, settings=cheap), build_dataset(bars, settings=costly)
    assert len(a) == len(b) and a
    assert all(rb["fwd"] < ra["fwd"] for ra, rb in zip(a, b))
