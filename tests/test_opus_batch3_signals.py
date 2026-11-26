"""Opus review batch 3: M1, M3, M9, M11."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import consensus, indicators
from app import signals as sigmod

ET = ZoneInfo("America/New_York")


def _bars(n, last_day):
    out = []
    for i in range(n):
        d = last_day - timedelta(days=n - 1 - i)
        c = 100 + i * 0.5
        out.append({"time": datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
                    "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000 + i})
    return out


def test_m1_completed_bars_drops_partial_today():
    at = datetime(2026, 11, 25, 11, 0, tzinfo=ET)  # Wed, session open
    bars = _bars(30, at.date())
    assert len(indicators.completed_bars(bars, at)) == 29
    after = datetime(2026, 11, 25, 17, 0, tzinfo=ET)
    assert len(indicators.completed_bars(bars, after)) == 30
    assert indicators.ml_features(bars, None, at)["date"] == "2026-11-24"


def test_m9_features_with_bench_has_rs():
    bars = _bars(150, datetime(2026, 11, 20).date())
    bench = _bars(150, datetime(2026, 11, 20).date())
    f = indicators.features(bars, bench)
    assert f["rs_63d"] is not None and f["rs_126d"] is not None
    assert indicators.features(bars)["rs_63d"] is None


def test_m3_ensure_returns_none_when_stale_and_refresh_fails():
    eng = sigmod.SignalService.__new__(sigmod.SignalService)
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    eng.latest = lambda s: {"symbol": s, "created_at": old}

    async def boom(**kw):
        raise RuntimeError("down")
    eng.refresh = boom
    assert asyncio.run(eng.ensure("AAA", 20)) is None


def test_m11_ml_unavailable_marked(test_settings):
    sig = {"symbol": "ZZZ", "close": 100.0, "atr": 2.0, "trend": "down", "strength": "strong",
           "ta_score": 0.6, "sent_score": 0.3, "sent_n": 6, "ml_p": None, "features": {}}
    res = consensus.evaluate({"side": "BUY", "limit_price": 100.0}, sig, test_settings)
    ml = [c for c in res["checks"] if c["name"] == "ML filter"]
    if test_settings.ml_enabled:
        assert "ML unavailable" in ml[0]["detail"]
    regime = [c for c in res["checks"] if c["name"] == "TA regime"][0]
    assert regime["ok"] is False  # no ML override of the downtrend block


def test_m12_news_unavailable_marked_not_blocking(test_settings):
    sig = {"symbol": "NNN", "close": 100.0, "atr": 2.0, "trend": "up", "strength": "strong",
           "ta_score": 0.6, "sent_score": None, "sent_n": 0, "ml_p": None, "features": {},
           "news_unavailable": True}
    res = consensus.evaluate({"side": "BUY", "limit_price": 100.0}, sig, test_settings)
    c = [c for c in res["checks"] if c["name"] == "Sentiment"][0]
    assert c["ok"] is True and "News unavailable" in c["detail"]


def test_m3_ensure_allow_stale_returns_latest_for_exits():
    eng = sigmod.SignalService.__new__(sigmod.SignalService)
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    eng.latest = lambda s: {"symbol": s, "created_at": old}

    async def boom(**kw):
        raise RuntimeError("down")
    eng.refresh = boom
    assert asyncio.run(eng.ensure("AAA", 20)) is None                       # BUY stays strict
    assert asyncio.run(eng.ensure("AAA", 20, allow_stale=True))["symbol"] == "AAA"   # exit evaluated
