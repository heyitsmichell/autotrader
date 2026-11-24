"""Regression tests for the news-veto false positives and the earnings / news-refresh fixes."""
from __future__ import annotations

import asyncio

import pytest

from app import news_events as ne
from app.llm import LLMError
from app.yfinance_service import YahooFinanceService
from tests.test_news_pipeline import (_FakeTicker, _checker, _engine, _fake_llm, _fake_yf, _ok, _seed,
                                      _store)


def _ev(symbol, title):
    return ne.classify_event(title, symbol)


@pytest.mark.parametrize("sym,title", [
    ("NFLX", "Is It Finally Time to Buy Netflix"),
    ("NVDA", "3 Reasons to Buy Nvidia Stock"),
    ("NVDA", "Top AI Stocks to Buy: Nvidia, AMD"),
])
def test_to_buy_is_not_an_acquisition(sym, title):
    assert _ev(sym, title) != "acquisition_target"


def test_real_takeovers_still_tag_the_target():
    assert _ev("AMAT", "Lam Research agrees to buy Applied Materials for $90B") == "acquisition_target"
    assert _ev("LRCX", "Lam Research agrees to buy Applied Materials for $90B") != "acquisition_target"
    assert _ev("AMAT", "Lam Research to acquire Applied Materials for $90B") == "acquisition_target"


def test_offer_and_merger_do_not_tag_the_acquirer():
    assert _ev("AVGO", "Broadcom makes takeover offer for VMware") != "acquisition_target"
    assert _ev("ON", "Synaptics Stockholders Approve Merger with onsemi") != "acquisition_target"
    assert _ev("AMAT", "Lam Research makes takeover offer for Applied Materials") == "acquisition_target"


def test_reversed_downgrade_does_not_veto():
    assert _ev("NVDA", "Bernstein reverses Nvidia downgrade") is None
    assert _ev("NVDA", "Despite downgrade, Nvidia rallies") is None
    assert _ev("NVDA", "Citi downgrades Nvidia") == "downgrade"


def test_downgrade_binds_to_the_clause_naming_the_symbol():
    t = "Goldman upgrades AMD, downgrades Intel"
    assert _ev("AMD", t) != "downgrade"
    assert _ev("INTC", t) == "downgrade"
    wrap = "Wall St Wrap: Nvidia, MDB win bullish calls as PEP and Starbucks face downgrades"
    assert _ev("MDB", wrap) != "downgrade"
    assert _ev("NVDA", "Nvidia falls as Citi downgrades") == "downgrade"


@pytest.mark.parametrize("sym,title", [
    ("AAPL", "Apple wins dismissal of DOJ antitrust lawsuit"),
    ("NVDA", "SEC drops investigation into Nvidia"),
    ("NVDA", "Nvidia cleared in SEC probe"),
    ("AAPL", "EU regulators clear Apple's App Store changes, no fine"),
    ("AAPL", "Apple sues former engineer"),
])
def test_resolved_or_plaintiff_is_not_an_investigation(sym, title):
    assert _ev(sym, title) != "investigation"


def test_investigation_still_vetoes():
    assert _ev("NVDA", "SEC opens investigation into Nvidia") == "investigation"
    assert _ev("AAPL", "Apple sued by shareholders over disclosures") == "investigation"


def test_insider_sale_is_not_an_offering_but_company_offering_is():
    assert _ev("NVDA", "Jensen Huang sells $15 million of Nvidia shares") != "offering"
    assert _ev("NVDA", "Nvidia announces $2 billion stock offering") == "offering"
    assert _ev("NVDA", "Nvidia prices secondary offering") == "offering"


def test_lexicon_does_not_punish_resolutions():
    assert ne.local_score("SEC drops investigation into Nvidia")[0] > -0.15
    assert ne.local_score("SEC opens investigation into Nvidia")[0] < -0.3


@pytest.mark.asyncio
async def test_llm_label_overrides_keyword_event_on_llm_rows(test_settings, test_db):
    c = _checker(test_settings, test_db)
    _store(test_db, "k", "Micron downgraded by BNP Paribas Exane", 5, scorer="llm",
           event_type="downgrade", llm_event="none")                  # LLM says: no event
    assert (await c.check("MU", "BUY", 100.0)).ok is True
    _store(test_db, "k2", "Micron downgraded by Citi", 4, scorer="local", event_type="downgrade")
    assert (await c.check("MU", "BUY", 100.0)).ok is False             # keyword rows unchanged


def test_effective_event_requires_llm_relevance():
    row = {"title": "x", "scorer": "llm", "llm_event": "bankruptcy", "llm_relevance": 0.3}
    assert ne.effective_event(row, "MU") is None
    assert ne.effective_event({**row, "llm_relevance": 0.8}, "MU") == "bankruptcy"


# --- earnings fail-open ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_calendar_and_no_history_is_unknown_not_clear(monkeypatch):
    _fake_yf(monkeypatch, cal={}, info={}, get_earnings_dates=lambda self, limit=12: None)
    y = YahooFinanceService(None)
    assert await y.get_earnings_dates("ADI") is None
    assert "ADI" not in y._earnings                                      # nothing cached for 12 h
    _fake_yf(monkeypatch, cal={}, info={"quoteType": "ETF"}, get_earnings_dates=lambda self, limit=12: None)
    assert await YahooFinanceService(None).get_earnings_dates("SPYM") == []
    assert _FakeTicker                                                    # (import keeps fixture module loaded)


# --- LLM output-format errors ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http400_json_validate_halves_batch_instead_of_pausing(test_settings, test_db, monkeypatch):
    e = _engine(test_settings, test_db, news_llm_batch_size=10, news_llm_max_batches=6)
    _seed(test_db, 10)
    err = LLMError("groq API returned HTTP 400: json_validate_failed", status_code=400)
    calls = _fake_llm(monkeypatch, lambda p, n: err if len(p) > 5 else _ok(p))
    res = await e.score_with_llm()
    assert [c["n"] for c in calls][:2] == [10, 5] and res["scored"] == 10
    assert e._llm_until == 0.0


@pytest.mark.asyncio
async def test_http400_other_still_pauses(test_settings, test_db, monkeypatch):
    e = _engine(test_settings, test_db)
    _seed(test_db, 4)
    _fake_llm(monkeypatch, lambda p, n: LLMError("HTTP 400: bad model", status_code=400))
    res = await e.score_with_llm()
    assert res["scored"] == 0 and e._llm_until > 0


# --- news refresh in the background -----------------------------------------------------------

@pytest.mark.asyncio
async def test_background_news_refresh_does_not_overlap_and_stamps_on_completion(test_settings, test_db):
    e = _engine(test_settings, test_db, news_refresh_minutes=15)
    gate = asyncio.Event()
    starts = []

    async def slow_google(sym, outer=None):
        starts.append(sym)
        await gate.wait()
        return []

    e._fetch_google = slow_google
    await e.refresh_background(["NVDA"], with_llm=False)
    for _ in range(5):
        await asyncio.sleep(0)
    await e.refresh_background(["NVDA"], with_llm=False)                  # overlapping call: no second fetch
    assert starts == ["NVDA"] and e._last_news_refresh == 0.0             # not stamped while running
    gate.set()
    await e._news_task
    assert e._last_news_refresh > 0 and starts == ["NVDA"]


@pytest.mark.asyncio
async def test_google_waiters_do_not_hold_shared_news_slots(test_settings, test_db):
    e = _engine(test_settings, test_db, news_google_concurrency=1)
    held = []

    async def fake_rss(sym, fetcher, label, pub):
        held.append(sym)
        await asyncio.sleep(0.05)
        return []

    e._fetch_rss = fake_rss
    outer = asyncio.Semaphore(2)
    t1 = asyncio.create_task(e._fetch_google("A", outer))
    t2 = asyncio.create_task(e._fetch_google("B", outer))
    await asyncio.sleep(0.01)
    assert held == ["A"] and outer._value == 1             # B queues behind Google WITHOUT a shared slot
    await asyncio.gather(t1, t2)
    assert held == ["A", "B"] and outer._value == 2


# --- Webull news: off switch and escalating breaker (UAT search_news is always 404) -------------
@pytest.mark.asyncio
async def test_webull_news_can_be_disabled(test_settings, test_db):
    e = _engine(test_settings, test_db, news_webull_enabled=False)
    called = []

    async def wb(sym):
        called.append(sym)
        return []

    async def none(*a, **k):
        return []
    e._fetch_webull, e._fetch_google = wb, none
    await e.refresh(["NVDA"], with_llm=False)
    assert called == []


def test_webull_breaker_pause_doubles_and_resets(test_settings, test_db):
    e = _engine(test_settings, test_db)
    err = RuntimeError("search_news: Server error (HTTP 404)")
    pauses = []
    for _ in range(3):
        for _ in range(e.WEBULL_BREAKER_THRESHOLD):
            e._webull_breaker_record(err)
        pauses.append(round(e._wb_breaker_until - __import__("time").monotonic()))
    assert pauses[0] == pytest.approx(1800, abs=5)
    assert pauses[1] == pytest.approx(3600, abs=5)
    assert pauses[2] == pytest.approx(7200, abs=5)
    e._webull_breaker_record(None)                      # one success resets the escalation
    for _ in range(e.WEBULL_BREAKER_THRESHOLD):
        e._webull_breaker_record(err)
    assert round(e._wb_breaker_until - __import__("time").monotonic()) == pytest.approx(1800, abs=5)
