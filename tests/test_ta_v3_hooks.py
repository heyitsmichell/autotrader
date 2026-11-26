import contextlib

from app.backtest import engine
from app.strategy_lab import harness, ta_v3_eval


def test_gate_worker_leaves_engine_untouched(monkeypatch, tmp_path):
    orig_sig, orig_hook = engine.signal_at, harness.entry_hook
    seen = {}

    @contextlib.contextmanager
    def fake_hook(*a, **kw):
        yield {"signal_fn": lambda sym, bl, f, i, s, **k: {"ok": False}, "portfolio_factory": None}

    def fake_run(variant, scope, n_trials=0):
        with harness.entry_hook(variant, tmp_path, None) as hooks:
            seen["during_engine"] = engine.signal_at
            bl = [{"time": __import__("datetime").datetime(2020, 1, i + 1), "close": 1.0} for i in range(3)]
            seen["res"] = hooks["signal_fn"]("X", bl, [{}, {}, {}], 1, None)
        return {}

    monkeypatch.setattr(harness, "entry_hook", fake_hook)
    monkeypatch.setattr(harness, "run_variant", fake_run)
    monkeypatch.setattr(harness, "data_cutoff", lambda src: (None, None))
    monkeypatch.setattr(harness, "research_dir", lambda src, c: tmp_path)
    import pandas as pd
    monkeypatch.setattr(ta_v3_eval.pd, "read_csv", lambda *a, **k: pd.DataFrame(
        {"date": pd.to_datetime(["2020-01-01"]), "close": [1.0]}))
    monkeypatch.setattr(ta_v3_eval, "registry_count", lambda: 1)
    monkeypatch.setattr(ta_v3_eval.os, "chdir", lambda p: None)
    ta_v3_eval._gate_worker({"scoring": "v3", "ta_min": 0.5, "exit_style": "fixed"})
    assert seen["during_engine"] is orig_sig and engine.signal_at is orig_sig
    assert seen["res"] == {"ok": False}
    assert harness.entry_hook is fake_hook      # restored to what it was before the worker
