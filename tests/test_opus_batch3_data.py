from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from app.ml_data.schema import is_holdout, HOLDOUT_START


def test_m8_holdout_exit_date():
    assert is_holdout("2025-01-01") == 0
    assert is_holdout("2025-01-01", "2099-01-01") == 1
    assert is_holdout("2025-01-01", None) == 0
    assert is_holdout(HOLDOUT_START) == 1


def test_m13_keyword_event_survives_llm_none():
    from app.news_events import effective_event
    it = {"scorer": "llm", "llm_event": "none", "llm_relevance": 0.9, "title": "ACME downgraded to sell by analyst"}
    assert effective_event(it, "ACME") is not None
    it2 = dict(it, llm_relevance=0.1)
    assert effective_event(it2, "ACME") is None


def test_m13_undated_not_fresh():
    from app.news_events import find_negative_event
    now = datetime.now(timezone.utc)
    it = {"title": "ACME downgraded to sell", "published_at": None,
          "fetched_at": now.isoformat(), "event_type": "downgrade"}
    assert find_negative_event("ACME", [it], now=now, hours=48) is None


def test_m12_event_veto_fails_closed():
    from app.eligibility import EligibilityChecker
    class DB:
        def news(self, *a, **k):
            raise RuntimeError("db down")
    obj = EligibilityChecker.__new__(EligibilityChecker)
    obj.db = DB()
    obj.s = SimpleNamespace(event_veto_hours=48)
    obj.signals = None
    r = obj._check_event_veto("ACME")
    assert not r.ok and "event veto unavailable" in r.reason


def test_m12_news_down_flag():
    from app.sentiment import SentimentEngine
    svc = SentimentEngine.__new__(SentimentEngine)
    svc._outage_alerted = True
    assert svc.news_down is True


def test_m10_live_rows_not_in_training(tmp_path, monkeypatch):
    from app import options_ml as om
    captured = {}
    def fake_fit(X, y, w):
        captured.setdefault("n", len(y))  # first fit = validation model
        k = X.shape[1]
        return np.zeros(k + 1), np.zeros(k), np.zeros(k), np.ones(k)
    monkeypatch.setattr(om, "_fit_scaled", fake_fit)
    n = len(om.ALL_FEATURES)
    Xb = np.zeros((100, n)); yb = np.array([0., 1.] * 50)
    dates = np.array(["2024-06-01"] * 70 + ["2025-06-01"] * 30)
    monkeypatch.setattr(om, "backtest_frame", lambda p: (Xb, yb, dates))
    ds = tmp_path / "d.csv"; ds.write_text("x")
    live = [{"features": {}, "delta": .3, "dte": 30, "iv": .3, "ret": 1.0, "closed_at": "2025-06-01"}] * 5
    monkeypatch.setattr(om, "_live_matrix", lambda rows: (np.zeros((len(rows), n)), np.ones(len(rows))))
    om.train(live, dataset=ds, save=False)
    assert captured["n"] == 70  # live rows dated in the validation period excluded


def test_m7_options_live_holdout_rows_excluded():
    from app import options_ml as om
    from app.ml_data.schema import HOLDOUT_START
    ok = {"closed_at": "2025-06-01", "opened_at": "2025-05-20"}
    bad_close = {"closed_at": HOLDOUT_START, "opened_at": "2025-06-01"}
    bad_open = {"closed_at": None, "opened_at": HOLDOUT_START + "T10:00:00"}
    assert om.drop_holdout_rows([ok, bad_close, bad_open]) == [ok]
