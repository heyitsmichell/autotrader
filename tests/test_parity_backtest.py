"""Backtest/live parity (QA H7, M8): ML on by default with walk-forward retraining, --no-ml for
rules-only, ML degrading cleanly without torch, printed limitations, earnings fail-closed."""
import asyncio
import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app import ml
from app.backtest import signals as bts
from app.backtest.signals import LIMITATIONS, build_ml_predictor, limitations_text, signal_at

ROOT = Path(__file__).resolve().parent.parent


def _bars(n=260, drift=0.001):
    from datetime import datetime, timedelta, timezone
    import math
    out, px = [], 100.0
    t0 = datetime(2023, 1, 2, tzinfo=timezone.utc)
    for i in range(n):
        px *= 1 + drift + 0.012 * math.sin(i / 5.0)
        out.append({"time": t0 + timedelta(days=i), "open": px, "high": px * 1.01,
                    "low": px * 0.99, "close": px, "volume": 5_000_000})
    return out


# ------------------------------------------------------------------ defaults
def test_backtest_ml_is_on_by_default(test_settings):
    assert test_settings.backtest_ml_enabled is True


def test_no_ml_means_no_predictor_and_rules_only(test_settings):
    s = replace(test_settings, backtest_ml_enabled=False)
    pred, status = build_ml_predictor(s, {"AAA": _bars()})
    assert pred is None and "rules-only" in status


def test_missing_torch_degrades_with_a_clear_message(test_settings, monkeypatch):
    monkeypatch.setattr(ml, "torch", None)
    monkeypatch.setattr(ml, "TORCH_ERROR", "PyTorch is not installed (test)")
    pred, status = build_ml_predictor(test_settings, {"AAA": _bars()})
    assert pred is None
    assert "UNAVAILABLE" in status and "PyTorch is not installed" in status and "RULES-ONLY" in status


def test_too_little_data_degrades_instead_of_crashing(test_settings):
    pred, status = build_ml_predictor(test_settings, {"AAA": _bars(40)})
    assert pred is None and "UNAVAILABLE" in status


def test_signal_at_uses_predictor_only_when_ml_enabled(test_settings):
    from app.indicators import feature_series
    bars = _bars()
    feats = feature_series(bars)
    calls = []

    def predictor(f):
        calls.append(f["date"])
        return 0.9

    base = replace(test_settings, eligibility_enabled=False)
    on = signal_at(bars, feats, 200, replace(base, backtest_ml_enabled=True), ml_predictor=predictor)
    assert on["signal"]["ml_p"] == 0.9 and len(calls) == 1
    off = signal_at(bars, feats, 200, replace(base, backtest_ml_enabled=False), ml_predictor=predictor)
    assert off["signal"]["ml_p"] is None and len(calls) == 1


# ------------------------------------------------------------------ walk-forward has no look-ahead
@pytest.mark.skipif(ml.torch is None, reason="PyTorch not installed")
def test_walkforward_predictor_trains_only_on_the_past(test_settings, monkeypatch):
    from app.indicators import feature_series
    bars = {sym: _bars(300, d) for sym, d in (("AAA", 0.001), ("BBB", 0.0005), ("CCC", 0.0))}
    fitted = []

    def fake_fit(rows, *a, **k):
        fitted.append((min(r["date"] for r in rows), max(r["date"] for r in rows)))
        return ("model", "mean", "std")

    monkeypatch.setattr(ml, "_fit", fake_fit)
    monkeypatch.setattr(ml, "_predict", lambda m, mean, std, xs: [0.6 for _ in xs])
    pred = ml.make_walkforward_predictor(test_settings, bars, refit_days=60, min_rows=100)
    feats = feature_series(bars["AAA"])
    assert pred(feats[290]) == 0.6
    assert pred(feats[291]) == 0.6 and len(fitted) == 1          # cached within the window
    day = feats[290]["date"]
    from datetime import date, timedelta
    bucket_start = date.fromordinal(date.fromisoformat(day).toordinal() // 60 * 60)
    assert fitted[0][1] < (bucket_start - timedelta(days=30)).isoformat()   # purged
    assert fitted[0][1] < day
    # an early day with too little history gets no model
    assert pred(feats[70]) is None


@pytest.mark.skipif(ml.torch is None, reason="PyTorch not installed")
def test_walkforward_predictor_rejects_tiny_history(test_settings):
    with pytest.raises(ml.MLUnavailable):
        ml.make_walkforward_predictor(test_settings, {"AAA": _bars(40)})


# ------------------------------------------------------------------ report limitations (M8)
def test_report_prints_the_backtest_limitations(test_settings):
    text = limitations_text(test_settings, "on: walk-forward")
    low = text.lower()
    assert "sentiment" in low and "first-15" in low and "earnings" in low
    assert "one position per symbol" in low
    assert len(LIMITATIONS) >= 4 and "walk-forward" in text


def test_pullback_gate_is_mirrored_in_the_backtest_signal(test_settings):
    from app.indicators import feature_series
    bars = _bars()
    feats = feature_series(bars)
    s = replace(test_settings, entry_pullback_gate=True, backtest_ml_enabled=False,
                eligibility_enabled=False)
    res = signal_at(bars, feats, 220, s)
    names = [c["name"] for c in res.get("gate", {}).get("checks", [])]
    assert "Pullback" in names
    assert "Pullback gate: on" in limitations_text(s)


# ------------------------------------------------------------------ the manual script
@pytest.mark.slow
def test_manual_script_help_runs():
    r = subprocess.run([sys.executable, str(ROOT / "run_backtest_manual.py"), "--help"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    assert "--no-ml" in r.stdout and "--compare" in r.stdout


def test_manual_script_flags_are_exclusive():
    r = subprocess.run([sys.executable, str(ROOT / "run_backtest_manual.py"), "--no-ml", "--compare"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0


def test_manual_script_respects_matrix_env_override():
    text = (ROOT / "run_backtest_manual.py").read_text(encoding="utf-8")
    assert "setdefault" in text and "'BACKTEST_ML_ENABLED'" not in text


@pytest.mark.slow
def test_run_backtest_manual_no_ml_forces_rules_only(test_settings, monkeypatch):
    spec = importlib.util.spec_from_file_location("rbm", ROOT / "run_backtest_manual.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    def fake_builder(settings, bars):
        seen["ml"] = settings.backtest_ml_enabled
        return None, "stub"

    monkeypatch.setattr(bts, "build_ml_predictor", fake_builder)
    broker = mod.MockBrokerForBacktest()
    # tiny universe (2 symbols + benchmark): the assertions only concern the ml flag routing
    s = replace(test_settings, backtest_ml_enabled=True, backtest_min_bars=60,
                universe_name="custom", universe_custom=["AAPL", "MSFT"], watchlist=["AAPL", "MSFT"],
                backtest_benchmark_symbol="SPY",
                backtest_data_dir=test_settings.backtest_data_dir)
    off, _ = asyncio.run(mod.run_backtest_manual(s, broker, ml=False))
    assert seen["ml"] is False and off.ml_status == "stub"
    on, _ = asyncio.run(mod.run_backtest_manual(s, broker, ml=None, loaded=(off.bars, off.errors)))
    assert seen["ml"] is True
    assert "Sentiment" in on.limitations
