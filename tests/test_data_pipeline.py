import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest
from app.broker import BrokerError
from types import SimpleNamespace
import numpy as np
from app.ml_data.schema import is_holdout, HOLDOUT_START

def _bars(n=30, close=100.0, vol=1_000_000.0, today_vol=1000.0):
    """n completed daily bars ending yesterday, plus a tiny partial bar for today."""
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out = [{"time": day - timedelta(days=n - i), "open": close, "high": close, "low": close,
            "close": close, "volume": vol} for i in range(n)]
    out.append({"time": day, "open": close, "high": close, "low": close, "close": close,
                "volume": today_vol})
    return out


class FakeSignals:
    def __init__(self, bars):
        self._bars = bars

    def cached_bars(self, symbol, max_age_seconds=1800):
        return self._bars


class FakeBroker:
    def __init__(self, snap=None, bars=None):
        self.snap, self.bars = snap, bars

    async def get_snapshot(self, symbol):
        return self.snap

    async def daily_bars(self, symbol, count):
        if self.bars is None:
            raise BrokerError("no bars")
        return self.bars


def _checker(settings, broker, signals):
    from app.eligibility import EligibilityChecker
    s = replace(settings, eligibility_min_volume=20_000_000.0)
    return EligibilityChecker(s, broker, signals)


class TestMinVolume:
    @pytest.mark.asyncio
    async def test_liquid_symbol_passes_with_tiny_partial_session(self, test_settings):
        # Today's snapshot volume is $237k, but 20d average is $100M.
        c = _checker(test_settings, FakeBroker({"price": 179.0, "volume": 1300.0}),
                     FakeSignals(_bars(close=100.0, vol=1_000_000.0)))
        assert (await c._check_min_volume("XLV")).ok is True

    @pytest.mark.asyncio
    async def test_illiquid_symbol_fails_with_new_message(self, test_settings):
        c = _checker(test_settings, FakeBroker({"price": 10.0, "volume": 5e9}),
                     FakeSignals(_bars(close=10.0, vol=10_000.0)))
        r = await c._check_min_volume("ILLQ")
        assert r.ok is False and "avg daily $ volume (20d)" in r.reason

    @pytest.mark.asyncio
    async def test_fetches_bars_when_not_cached_and_fails_closed_without_data(self, test_settings):
        c = _checker(test_settings, FakeBroker(None, _bars()), FakeSignals(None))
        assert (await c._check_min_volume("XLV")).ok is True
        c = _checker(test_settings, FakeBroker(None, None), FakeSignals(None))
        r = await c._check_min_volume("XLV")
        assert r.ok is False and "data unavailable" in r.reason


class TestCallSpacer:
    @pytest.mark.asyncio
    async def test_calls_are_spaced(self):
        from app.broker.transport import CallSpacer
        t = [0.0]
        slept = []

        async def sleep(d):
            slept.append(d)
            t[0] += d

        sp = CallSpacer({"get_stock_bars": 0.5}, clock=lambda: t[0], sleep=sleep)
        waits = [await sp.acquire("get_stock_bars") for _ in range(3)]
        assert waits == [0.0, 0.5, 0.5]
        assert await sp.acquire("other_tool") == 0.0


class SigBroker:
    connected = True

    def __init__(self):
        self.calls = []

    async def daily_bars(self, sym, count):
        self.calls.append((sym, count))
        if sym == "QRVO":
            raise BrokerError("get_stock_bars: Server error (HTTP 417): [INVALID_SYMBOL] "
                              "The symbols does not exist in the category. [QRVO].")
        return [{k: v for k, v in b.items()} for b in _bars(80)]


class StubSentiment:
    async def refresh(self, symbols, with_llm=None):
        pass


def _service(settings, db, broker):
    from app.signals import SignalService
    svc = SignalService(settings, broker, db, StubSentiment(), None)
    svc._store = lambda run_id, sym, bars: {"symbol": sym, "n": len(bars)}
    return svc


class TestInvalidSymbolAndCaching:
    @pytest.mark.asyncio
    async def test_invalid_symbol_negative_cached_and_logged_once(self, test_settings, test_db):
        broker = SigBroker()
        svc = _service(test_settings, test_db, broker)
        out = await svc.refresh(symbols=["AAPL", "QRVO"])
        assert [r["symbol"] for r in out] == ["AAPL"]
        await svc.refresh(symbols=["AAPL", "QRVO"])
        assert [c for c in broker.calls if c[0] == "QRVO"] == [("QRVO", 260)]
        warns = [e for e in test_db.events(limit=200) if "QRVO" in e["message"]] \
            if hasattr(test_db, "events") else []
        assert len(warns) <= 1

    @pytest.mark.asyncio
    async def test_daily_bars_not_refetched_within_ttl_then_incremental(self, test_settings, test_db):
        broker = SigBroker()
        svc = _service(test_settings, test_db, broker)
        await svc.refresh(symbols=["AAPL"])
        await svc.refresh(symbols=["AAPL"])
        assert broker.calls == [("AAPL", 260)]
        svc.BARS_TTL_SECONDS = 0
        await svc.refresh(symbols=["AAPL"])
        assert broker.calls[-1] == ("AAPL", svc.BARS_INCREMENT)

    @pytest.mark.asyncio
    async def test_deadline_keeps_partial_results(self, test_settings, test_db):
        class Slow(SigBroker):
            async def daily_bars(self, sym, count):
                if sym == "SLOW":
                    await asyncio.sleep(30)
                return await super().daily_bars(sym, count)

        svc = _service(test_settings, test_db, Slow())
        svc.REFRESH_DEADLINE_SECONDS = 0.3
        out = await svc.refresh(symbols=["AAPL", "SLOW"])
        assert [r["symbol"] for r in out] == ["AAPL"]
        assert svc.refreshing is False


class TestNewsBreaker:
    @staticmethod
    def _engine(settings, db):
        from app.sentiment import SentimentEngine

        class B:
            calls = 0

            async def call(self, name, **kw):
                B.calls += 1
                raise BrokerError(f"{name}: Server error (HTTP 500): [SDK.UnknownServerError] "
                                  f"No message available")
        e = SentimentEngine(settings, B(), db)
        return e, B

    @pytest.mark.asyncio
    async def test_breaker_opens_after_k_failures_and_recovers(self, test_settings, test_db):
        e, B = self._engine(test_settings, test_db)
        for sym in ["AAA", "BBB", "CCC", "DDD", "EEE"]:
            with pytest.raises(BrokerError):
                await e._fetch_webull(sym)
        assert e._webull_breaker_open()
        assert len([x for x in test_db.events(limit=200) if "Webull news is failing" in x["message"]]) == 1
        before = B.calls
        assert await e._fetch_webull("FFF") == []
        assert B.calls == before                       # no broker call while open
        e._wb_breaker_until = 0.0                      # cooldown elapsed
        with pytest.raises(BrokerError):
            await e._fetch_webull("GGG")
        assert B.calls > before


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
