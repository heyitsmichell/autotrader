import dataclasses
import os
from datetime import date, datetime, timezone
from types import SimpleNamespace
from app.backtest.costs import CostModel
from app.backtest import engine
from app.backtest.data import BarStore
from app.backtest.engine import Portfolio, run_backtest, validate_backtester, _synthetic_bars
from app.config import load_settings
import ast
import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pytest
from app.backtest import walkforward as wf
from app.backtest.signals import bench_close_asof, relative_strength_pct
from app.broker import bar_day

def _settings(**kw):
    base = dict(
        backtest_commission_per_order=0.0, backtest_slippage_bps=0.0, backtest_atr_slippage_mult=0.0,
        backtest_impact_bps=0.0, backtest_max_impact_bps=0.0, backtest_max_gross_exposure_pct=100.0,
        backtest_max_hold_days=0, backtest_exit_on_trend_break=False, exit_on_trend_break_enabled=False,
        backtest_ml_enabled=False, backtest_live_exits=False, backtest_min_bars=3,
        backtest_initial_cash=10_000.0, max_order_qty=1000, max_order_usd=50_000.0,
        max_position_usd=50_000.0, max_risk_per_trade_usd=500.0, max_daily_loss_usd=0.0)
    base.update(kw)
    return dataclasses.replace(load_settings(), **base)


def _bar(d, o, h, l, c):
    return {"time": datetime(2020, 1, d, tzinfo=timezone.utc), "open": o, "high": h, "low": l,
            "close": c, "volume": 1_000_000}


# ---- 11: time stop counts trading bars ------------------------------------------------------
def test_time_stop_counts_trading_bars_not_calendar_days():
    s = _settings(backtest_max_hold_days=2)
    p = Portfolio(s, cash=10_000.0)
    cm = CostModel.from_settings(s)
    fri = date(2020, 1, 3)
    p.on_bar_start(fri, {})
    p.fill_buy("X", 10, 100.0, fri, 1.0, 50.0, None, "up", 1.0, None, 0.0, 0.0)
    flat = lambda: {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    p.on_bar_start(date(2020, 1, 6), {})          # Monday: 3 calendar days, 1 trading bar
    assert p.check_exits("X", flat(), date(2020, 1, 6), cm, 1e9) is None
    p.on_bar_start(date(2020, 1, 7), {})          # 2 trading bars held
    res = p.check_exits("X", flat(), date(2020, 1, 7), cm, 1e9)
    assert res is not None and res[2] == "time"


# ---- 12: cash never negative when fill > sizing limit ----------------------------------------
def test_marketable_fill_above_limit_does_not_overspend(tmp_path):
    bars = [_bar(1, 100, 101, 99, 100), _bar(2, 100, 102, 99.5, 101), _bar(3, 108, 109, 107, 108),
            _bar(4, 108, 109, 107, 108), _bar(5, 108, 109, 107, 108)]
    BarStore(tmp_path).save("TEST", bars)
    s = _settings(backtest_live_exits=True, entry_order_type="marketable", price_band_pct=10.0)
    grabbed = []

    def sig(symbol, blist, feats, index, settings, **kw):
        if index != 1:
            return {"ok": False}
        return {"ok": True, "sl": 99.0, "tp": None,
                "signal": {"close": 101.0, "atr": 1.0, "ta_score": 1.0, "trend": "up", "ml_p": None}}

    def factory(settings, cash):
        p = Portfolio(settings, cash=cash)
        grabbed.append(p)
        return p

    run_backtest(s, universe=["TEST"], benchmark_symbol="TEST", csv_dir=tmp_path,
                 signal_fn=sig, portfolio_factory=factory)
    p = grabbed[0]
    assert p.cash >= 0.0
    pos = p.positions["TEST"]
    assert pos["entry_price"] == 108.0
    assert pos["qty"] * 108.0 <= 10_000.0 and pos["qty"] < 99   # 99 sized at 101 would cost 10692


# ---- 13: validate_backtester is real and leaves os.environ alone -------------------------------
def test_validate_backtester_real_and_env_clean():
    before = dict(os.environ)
    out = validate_backtester()
    assert out["validation"] == "ok" and out["trades"] == 1 and out["pnl"] < 0
    assert dict(os.environ) == before


# ---- 14: hooks default to current behaviour -----------------------------------------------------
def test_hooked_run_matches_default(tmp_path):
    bars = _synthetic_bars(120, trend=0.002, seed=7)
    for sym in ("AAA", "SPY"):
        BarStore(tmp_path).save(sym, bars)
    s = _settings(consensus_ta_min=-1.0, eligibility_enabled=False)
    base = run_backtest(s, universe=["AAA"], benchmark_symbol="SPY", csv_dir=tmp_path)
    calls = []

    def sig(symbol, blist, feats, index, settings, **kw):
        calls.append(symbol)
        return engine.signal_at(blist, feats, index, settings, **kw)

    hooked = run_backtest(s, universe=["AAA"], benchmark_symbol="SPY", csv_dir=tmp_path, signal_fn=sig,
                          portfolio_factory=lambda st, cash: Portfolio(st, cash=cash))
    assert calls and set(calls) == {"AAA"}
    assert hooked.equity_curve == base.equity_curve
    assert [(t.symbol, t.entry_day, t.exit_day, t.pnl) for t in hooked.trades] == \
           [(t.symbol, t.entry_day, t.exit_day, t.pnl) for t in base.trades]


# ---- 15: run_variant logs the holdout peek -------------------------------------------------------
def test_run_variant_holdout_logs_peek(monkeypatch):
    from app.strategy_lab import harness
    seen = []
    monkeypatch.setattr(harness, "log_holdout", lambda name, h, *a, **k: seen.append((name, h)) or 2)

    def boom(src):
        raise RuntimeError("stop here")
    monkeypatch.setattr(harness, "data_cutoff", boom)
    name = next(iter(harness.REGISTRY))
    cwd = os.getcwd()
    try:
        harness.run_variant(name, scope="holdout")
    except RuntimeError:
        pass
    assert len(seen) == 1 and seen[0][0] == name
    assert os.getcwd() == cwd                     # no chdir side effect
    seen.clear()
    try:
        harness.run_variant(name, scope="research")
    except RuntimeError:
        pass
    assert seen == []


# ---- 16: autopsy thresholds from settings, units, meaningful stop check --------------------------
def test_autopsy_thresholds_from_settings():
    from app import autopsy
    trade = {"entry_fill_price": 100.0, "stop_price": 97.0, "entry_atr": 1.5,    # 2.0 ATR
             "entry_ta_score": 0.4, "entry_ml_p": 0.6}
    names = lambda cs: {c.cause for c in cs}
    assert "stop_placement_appropriate" in names(autopsy.analyze_stop_placement(trade))
    assert "stop_too_tight" in names(autopsy.analyze_stop_placement(trade, SimpleNamespace(sl_atr_mult=4.0)))
    assert "weak_ta_signal" not in names(autopsy.analyze_signal_quality(trade))
    strict = SimpleNamespace(consensus_ta_min=0.5, ml_min_p=0.7)
    got = names(autopsy.analyze_signal_quality(trade, strict))
    assert {"weak_ta_signal", "low_ml_probability"} <= got


def test_stop_protected_downside_is_meaningful():
    from app import autopsy
    base = {"exit_reason": "stop", "entry_fill_price": 100.0, "stop_price": 98.0, "mae": -2.1, "mfe": 0.5}
    ok = autopsy.analyze_mae_mfe({**base, "realized_pnl_pct": -2.05})
    assert "stop_protected_downside" in {c.cause for c in ok}
    gapped = autopsy.analyze_mae_mfe({**base, "realized_pnl_pct": -6.0, "mae": -6.5})
    assert "stop_protected_downside" not in {c.cause for c in gapped}


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
