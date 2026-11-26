"""Tests for strategy fixes: consensus RSI, walk-forward, sector map, relative strength."""
from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backtest import walkforward as wf
from app.backtest.signals import bench_close_asof, relative_strength_pct
from app.broker import bar_day


# --- consensus RSI (9) -------------------------------------------------------
def test_consensus_reads_rsi_from_features():
    src = inspect.getsource(__import__("app.consensus", fromlist=["x"]))
    assert 'feats.get("rsi14")' in src and "json.loads(feats)" in src   # dict or JSON-string features


def test_consensus_oversold_branch_allows_weak_downtrend():
    from app import consensus
    class _S:
        def __getattr__(self, name):
            if name in ("consensus_ta_min",):
                return 0.2
            if name in ("ml_min_p", "ml_first_ta_floor"):
                return 0.5
            return False
    s = _S()
    sig = {"trend": "down", "strength": "weak", "ta_score": 0.5, "features": {"rsi14": 20.0},
           "ml_p": None, "sent_n": 0}
    res = consensus.evaluate({"side": "BUY", "limit_price": 10.0}, sig, s)
    checks = res.get("checks") if isinstance(res, dict) else getattr(res, "checks", [])
    reg = [c for c in checks if (c.get("name") if isinstance(c, dict) else c.name) == "TA regime"]
    assert reg and (reg[0]["ok"] if isinstance(reg[0], dict) else reg[0].ok)
    sig["features"]["rsi14"] = 50.0
    res = consensus.evaluate({"side": "BUY", "limit_price": 10.0}, sig, s)
    checks = res.get("checks") if isinstance(res, dict) else getattr(res, "checks", [])
    reg = [c for c in checks if (c.get("name") if isinstance(c, dict) else c.name) == "TA regime"]
    assert not (reg[0]["ok"] if isinstance(reg[0], dict) else reg[0].ok)


# --- walk-forward (10) -------------------------------------------------------
def test_bh_known_example():
    p = [0.01, 0.04, 0.03, 0.005]
    rej, adj = wf.benjamini_hochberg(p, 0.05)
    # sorted: .005,.01,.03,.04 -> *4/rank: .02,.02,.04,.04 ; monotone
    assert adj == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert all(rej)
    # non-monotone case: [0.01, 0.02, 0.9] -> raw adj [.03,.03,.9]
    _, adj2 = wf.benjamini_hochberg([0.9, 0.01, 0.02], 0.05)
    assert adj2 == pytest.approx([0.9, 0.03, 0.03])
    # cumulative min must propagate: [0.04, 0.01] -> adj [0.04, 0.02] sorted .01*2/1=.02, .04*2/2=.04
    _, adj3 = wf.benjamini_hochberg([0.04, 0.011, 0.012], 0.05)
    assert max(adj3) <= 0.04 + 1e-12 and adj3[1] <= adj3[2] <= adj3[0]


def test_overlap_is_per_symbol_by_date_distance():
    # Different symbols never overlap; same symbol within horizon does.
    keys = [("A", 0), ("B", 0), ("A", 1), ("A", 10)]
    w = wf.compute_label_overlap_weights(keys, 5)
    assert w == pytest.approx([0.5, 1.0, 0.5, 1.0])
    kept = wf.subsample_non_overlapping(keys, 5)
    assert ("A", 1) not in kept and ("B", 0) in kept and ("A", 10) in kept


def _rows(n_days=160, syms=("A", "B", "C", "D")):
    rows = []
    d0 = date(2024, 1, 1)
    for i in range(n_days):
        for j, s in enumerate(syms):
            y = 1.0 if (i * 7 + j * 3) % 3 == 0 else 0.0
            rows.append({"date": d0 + timedelta(days=i), "symbol": s, "x": [float(i % 5)],
                         "y": y, "fwd": 0.01 if y else -0.01, "ta": 1.0})
    return rows


def _cfg(**kw):
    base = dict(n_folds=3, min_train_samples=40, min_test_samples=5, embargo_bars=5, purge_bars=5,
                min_folds=2, min_total_test_trades=10, min_independent_test_trades=5,
                multiple_testing_method="fdr", min_auc=0.0)
    base.update(kw)
    return wf.WalkForwardConfig(**base)


def test_walk_forward_no_input_mutation_and_checks():
    rows = _rows()
    res = wf.walk_forward(rows, _cfg(), ta_min=0.0, p_min=0.0,
                          model_factory=lambda tr: None,
                          predict_fn=lambda m, te: [0.5] * len(te))
    assert all("p" not in r for r in rows)
    chk = res.details["checks"]
    assert "multiple_testing" in chk and chk["no_leakage"] is True
    assert res.folds
    # independent trades counted per symbol by date distance: <= rows/embargo-ish, and > 0
    assert 0 < res.total_independent_test_trades <= res.total_test_trades
    # multiple-testing failing must fail the verdict
    if not chk["multiple_testing"]:
        assert res.passed is False


def test_walk_forward_leakage_check_not_faked():
    # embargo larger than purge makes no difference to purge; but embargo>0 guarantees gap
    res = wf.walk_forward(_rows(), _cfg(embargo_bars=8, purge_bars=8), 0.0, 0.0,
                          model_factory=lambda tr: None,
                          predict_fn=lambda m, te: [0.5] * len(te))
    assert res.details["checks"]["no_leakage"] is True


# --- sector map (11) ---------------------------------------------------------
def _sector_literal():
    src = Path("app/backtest/engine.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SECTOR_MAP":
            return node.value
    raise AssertionError("SECTOR_MAP not found")


def test_sector_map_no_duplicate_keys():
    keys = [k.value for k in _sector_literal().keys]
    dups = {k for k in keys if keys.count(k) > 1}
    assert not dups


def test_sector_map_spot_checks():
    from app.backtest.engine import SECTOR_MAP
    assert SECTOR_MAP["JNJ"] == "Healthcare"
    assert SECTOR_MAP["VRTX"] == "Healthcare"
    assert SECTOR_MAP["ISRG"] == "Healthcare"
    assert SECTOR_MAP["ODFL"] == "Industrials"
    assert SECTOR_MAP["CTAS"] == "Industrials"
    assert SECTOR_MAP["SPGI"] == "Financials"
    assert SECTOR_MAP["TSLA"] == "Consumer Discretionary"
    assert SECTOR_MAP["NFLX"] == "Communication Services"


# --- relative strength (13) --------------------------------------------------
def test_relative_strength_live_equals_backtest():
    lookback = 5
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    n = 30
    bars = [{"time": base + timedelta(days=i), "close": 100 + i * 1.5 + (i % 3)} for i in range(n)]
    bench = [{"time": base + timedelta(days=i), "close": 400 + i * 0.5} for i in range(n)]
    i = n - 1
    bt = relative_strength_pct(bars[i]["close"], bars[i - lookback]["close"],
                               bench[i]["close"], bench[i - lookback]["close"])
    b_now = bench_close_asof(bench, bar_day(bars[-1]["time"]), bar_day)
    b_then = bench_close_asof(bench, bar_day(bars[-1 - lookback]["time"]), bar_day)
    live = relative_strength_pct(bars[-1]["close"], bars[-1 - lookback]["close"], b_now, b_then)
    assert live == pytest.approx(bt)
    expected = ((bars[i]["close"] / bars[i - lookback]["close"] - 1)
                - (bench[i]["close"] / bench[i - lookback]["close"] - 1)) * 100
    assert bt == pytest.approx(expected)
