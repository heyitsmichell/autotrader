"""News pipeline: query building, Google 429 breaker, relevance, stale/dedupe, scoring,
weighting, hard-event classification + eligibility veto, earnings dates. No network."""
import json
import sys
import time
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import news_events as ne
from app.broker import BrokerError
from app.eligibility import EligibilityChecker
from app.sentiment import SentimentEngine
from app.yfinance_service import YahooFinanceService


def _now():
    from app.market import now_utc            # the suite freezes the clock; use the app's
    return now_utc()


def _iso(hours_ago: float) -> str:
    return (_now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _rss(items: list[tuple[str, str, float]]) -> str:
    """RSS 2.0 text from (title, publisher, hours_ago)."""
    from email.utils import format_datetime
    body = "".join(
        f"<item><title>{t}</title><link>https://x/{i}</link>"
        f"<pubDate>{format_datetime(_now() - timedelta(hours=h))}</pubDate>"
        f"<source>{pub}</source></item>" for i, (t, pub, h) in enumerate(items))
    return f"<rss><channel>{body}</channel></rss>"


class DeadBroker:
    async def call(self, name, **kw):
        raise BrokerError(f"{name}: down")


def _engine(settings, db, **over):
    base = dict(sentiment_rss_fallback=True, sentiment_yahoo_rss=False, news_refresh_minutes=0,
                sentiment_llm=False, news_rss_spacing_seconds=0.0, sentiment_source="blend",
                news_nasdaq_enabled=False, news_seekingalpha_enabled=False, news_bing_enabled=False)
    s = replace(settings, **{**base, **over})
    return SentimentEngine(s, DeadBroker(), db)


# --- query building ------------------------------------------------------------------------

def test_query_is_quoted_ticker_plus_stock_even_if_env_ate_the_space():
    assert ne.build_news_query("NVDA", " stock") == '"NVDA" stock'
    assert ne.build_news_query("nvda", "stock") == '"NVDA" stock'      # leading space lost by the env parser
    assert ne.build_news_query("NVDA", "") == '"NVDA"'
    assert ne.build_news_query("ON", " stock", "onsemi") == '"onsemi" stock'


def test_google_url_uses_quoted_query_and_window(test_settings, test_db):
    e = _engine(test_settings, test_db, sentiment_rss_query_suffix="stock",
                sentiment_rss_url="https://news.google.com/rss/search?q={symbol}+stock&hl=en-US&gl=US&ceid=US:en")
    url = e.google_url("NVDA")
    assert "q=%22NVDA%22+stock+when%3A3d&" in url            # not NVDAstock, not "stock stock"
    assert "NVDAstock" not in url and "stock+stock" not in url
    assert "q=%22onsemi%22+stock" in e.google_url("ON")        # word-like ticker: search by name


# --- Google pacing and 429 breaker ---------------------------------------------------------

def _status_error(code: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://news.google.com/rss/search")
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req, headers=headers))


@pytest.mark.asyncio
async def test_429_opens_breaker_once_and_pauses_google(test_settings, test_db):
    e = _engine(test_settings, test_db)
    calls = []

    async def fake(url):
        calls.append(url)
        raise _status_error(429)
    e._http_text = fake
    assert await e._fetch_google("NVDA") == []
    assert e._google_breaker_open()
    assert await e._fetch_google("AMD") == []              # no request while open
    assert await e._fetch_google("MU") == []
    assert len(calls) == 1
    warns = [x for x in test_db.events(limit=50) if "Google News rate-limited" in x["message"]]
    assert len(warns) == 1
    e._google_until = 0.0                                   # cooldown over
    calls.clear()
    e._http_text = lambda url: _async(_rss([("Nvidia beats estimates", "Reuters", 1)]))
    assert len(await e._fetch_google("NVDA")) == 1


async def _async(v):
    return v


@pytest.mark.asyncio
async def test_non_429_error_does_not_open_breaker(test_settings, test_db):
    e = _engine(test_settings, test_db)

    async def fake(url):
        raise _status_error(500)
    e._http_text = fake
    with pytest.raises(httpx.HTTPStatusError):
        await e._fetch_google("NVDA")
    assert not e._google_breaker_open()


@pytest.mark.asyncio
async def test_rss_requests_are_spaced(test_settings, test_db):
    e = _engine(test_settings, test_db)
    e.s = replace(e.s, news_rss_spacing_seconds=0.05)
    stamps = []

    async def fake(url):
        stamps.append(time.monotonic())
        return _rss([])
    e._http_text = fake
    import asyncio
    await asyncio.gather(*(e._rss_news(s) for s in ("A", "B", "C")))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.04 for g in gaps)


# --- relevance -----------------------------------------------------------------------------

@pytest.mark.parametrize("sym,title,expected", [
    ("NVDA", "NVDA stock jumps on AI demand", True),
    ("NVDA", "Nvidia beats estimates", True),
    ("NVDA", "Why AMD stock fell today", False),
    ("LRCX", "Applied Materials earnings preview - Yahoo Finance", False),     # AMAT story under LRCX
    ("INTC", "Stock market today: Dow, S&P 500 rise as oil falls", False),      # market recap
    ("ON", "Stocks to watch ON Monday", False),                                  # the word 'ON'
    ("ON", "Markets are ON edge", False),
    ("ON", "onsemi raises outlook as EV demand returns", True),
    ("ON", "ON Semiconductor (NASDAQ: ON) wins design contract", True),
    ("ARM", "Why Arm Holdings stock soared", True),
    ("ARM", "Tariffs hit ARM stock?", False),                                    # ticker alone is not enough
    ("MU", "Micron prices public offering", True),
    ("MU", "MU players and wrong stocks", False),
    ("NOW", "Rates fall NOW, analysts say", False),
    ("NOW", "ServiceNow (NYSE:NOW) tumbles after earnings", True),
    ("ALAB", "Astera Labs surges on data center demand", True),
    ("ALAB", "ALAB stock rallies", False),                                       # ambiguous: needs the name
    ("AAPL", "Apple's iPhone sales rise", True),
    ("AAPL", "Is AAPL a buy?", True),
    ("AAPL", "Pineapple prices rise", False),
])
def test_relevance_filter(sym, title, expected):
    assert (ne.relevance(sym, title) > 0) is expected


def test_relevance_ignores_summary_by_default_and_unknown_symbol_uses_ticker():
    assert ne.relevance("AAPL", "Roundup of tech stocks", "Apple and Microsoft rose") == 0.0
    assert ne.relevance("AAPL", "Roundup", "Apple rose", use_summary=True) == 0.5
    assert ne.relevance("ZZZZ", "ZZZZ announces results") > 0 and ne.relevance("ZZZZ", "zzzz in lowercase") == 0
    ne.register_name("QRVOX", "Qorvox Technologies, Inc.")
    assert ne.relevance("QRVOX", "Qorvox Technologies lands deal") > 0


# --- ingest: stale drop, relevance, dedupe across sources ---------------------------------

@pytest.mark.asyncio
async def test_refresh_drops_stale_and_irrelevant_and_dedupes(test_settings, test_db):
    e = _engine(test_settings, test_db, sentiment_yahoo_rss=True)
    google = _rss([("Nvidia beats estimates, raises guidance - Reuters", "Reuters", 2),
                   ("Nvidia stock in 2009 retrospective", "Old Blog", 24 * 400),     # stale
                   ("Why AMD stock fell today", "Other", 3),                          # irrelevant
                   ("Nvidia faces SEC probe over accounting", "CNBC", 1)])
    yahoo = _rss([("Nvidia beats estimates, raises guidance", "Yahoo Finance", 2),    # same story, no publisher suffix
                  ("Nvidia unveils new chip", "Yahoo Finance", 5)])

    async def g(url): return google
    async def y(url): return yahoo
    e._rss_news, e._yahoo_news = (lambda sym: g(sym)), (lambda sym: y(sym))
    await e.refresh(["NVDA"], with_llm=False)
    rows = test_db.news("NVDA", limit=50)
    titles = sorted(r["title"] for r in rows)
    assert len(rows) == 3, titles
    assert not any("2009" in t or "AMD" in t for t in titles)
    assert sum("beats estimates" in t for t in titles) == 1
    st = e.ingest_stats
    assert st["stale"] == 1 and st["irrelevant"] == 1 and st["duplicate"] == 1 and st["stored"] == 3
    # everything stored is scored right away (no LLM needed) and tagged with its scorer
    assert all(r["scorer"] == "local" and r["llm_score"] is not None for r in rows)
    s = e.summary("NVDA")
    assert s["n"] == 3 and s["n_relevant"] == 3 and s["pending"] == 0
    assert any(ev["event"] == "investigation" and ev["negative"] for ev in s["events"])


def test_title_key_collapses_publisher_suffix():
    from app.sentiment import title_key
    assert title_key("Nvidia beats estimates - Reuters") == title_key("Nvidia beats estimates")


# --- local scorer ---------------------------------------------------------------------------

def test_local_scorer_directions_and_negation():
    pos, lab = ne.local_score("Nvidia beats estimates, raises guidance; shares surge to record high")
    neg, nlab = ne.local_score("Intel misses estimates, cuts outlook; stock plunges")
    assert pos > 0.4 and lab == "bullish" and neg < -0.4 and nlab == "bearish"
    assert ne.local_score("Company announces annual meeting date") == (0.0, "neutral")
    assert ne.local_score("Nvidia stock is down today")[0] < 0
    assert ne.local_score("Nvidia stock is up today")[0] > 0
    assert ne.local_score("Nvidia dismisses lawsuit fears: no lawsuit filed")[0] > ne.local_score("Nvidia hit by lawsuit fears")[0]
    assert all(-0.8 <= ne.local_score(t)[0] <= 0.8 for t in ("surge " * 30, "plunge " * 30))


@pytest.mark.asyncio
async def test_score_local_scores_everything_and_filters_irrelevant(test_settings, test_db):
    e = _engine(test_settings, test_db)
    for i, t in enumerate(["Nvidia beats estimates", "Totally unrelated AMD story"]):
        test_db.upsert_news({"news_id": f"n{i}", "symbol": "NVDA", "title": t, "summary": "", "source": "Reuters",
                             "url": None, "published_at": _iso(1), "sentiment": None})
    out = e.score_local()
    assert out == {"local": 1, "filtered": 1}
    rows = {r["news_id"]: r for r in test_db.news("NVDA", limit=10)}
    assert rows["n0"]["scorer"] == "local" and rows["n0"]["llm_score"] > 0
    assert rows["n1"]["scorer"] == "filter"
    assert test_db.unscored_news(e._since()) == []
    assert e.summary("NVDA")["n"] == 1                     # the filtered row is not counted


# --- LLM batch scoring ----------------------------------------------------------------------

def _seed(db, n, symbol="NVDA"):
    for i in range(n):
        db.upsert_news({"news_id": f"h{i}", "symbol": symbol, "title": f"Nvidia headline number {i}",
                        "summary": "", "source": "Reuters", "url": None,
                        "published_at": _iso(1 + i / 100), "sentiment": None})


def _fake_llm(monkeypatch, behaviour):
    """behaviour(refs_in_prompt, call_no) -> dict|Exception; response shaped like Groq's."""
    calls = []

    async def fake(settings, *, messages, json_schema, max_tokens, **kw):
        user = messages[-1]["content"]
        payload = json.loads(user.split("\n", 1)[1])
        calls.append({"n": len(payload), "max_tokens": max_tokens})
        out = behaviour(payload, len(calls))
        if isinstance(out, Exception):
            raise out
        return {"choices": [{"message": {"content": out}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "model": "m"}
    monkeypatch.setattr("app.sentiment.chat_completion", fake)
    return calls


def _ok(payload, event="none"):
    return json.dumps({"items": [{"ref": p["ref"], "label": "bullish", "score": 0.6, "relevance": 0.9,
                                  "event": event, "reason": "ok"} for p in payload]})


@pytest.mark.asyncio
async def test_llm_scores_in_small_batches_and_marks_scorer(test_settings, test_db, monkeypatch):
    e = _engine(test_settings, test_db, news_llm_batch_size=10, news_llm_max_batches=6)
    _seed(test_db, 25)
    calls = _fake_llm(monkeypatch, lambda p, n: _ok(p))
    res = await e.score_with_llm()
    assert [c["n"] for c in calls] == [10, 10, 5] and res["scored"] == 25 and res["batches"] == 3
    assert all(c["max_tokens"] >= 1500 + 120 * c["n"] for c in calls)      # headroom: JSON is never cut off
    rows = test_db.news("NVDA", limit=100)
    assert all(r["scorer"] == "llm" and r["llm_score"] == 0.6 for r in rows)


@pytest.mark.asyncio
async def test_llm_truncated_json_halves_the_batch(test_settings, test_db, monkeypatch):
    e = _engine(test_settings, test_db, news_llm_batch_size=10, news_llm_max_batches=6)
    _seed(test_db, 10)
    calls = _fake_llm(monkeypatch, lambda p, n: '{"items": [{"ref": 1, "label": "bul' if len(p) > 5 else _ok(p))
    res = await e.score_with_llm()
    assert [c["n"] for c in calls][:2] == [10, 5] and res["scored"] == 10


@pytest.mark.asyncio
async def test_llm_failure_pauses_and_keeps_local_scores(test_settings, test_db, monkeypatch):
    from app.llm import LLMError
    e = _engine(test_settings, test_db)
    _seed(test_db, 4)
    calls = _fake_llm(monkeypatch, lambda p, n: LLMError("All configured LLM routes failed"))
    res = await e.score_with_llm()
    assert res["scored"] == 0 and len(calls) == 1
    assert all(r["scorer"] == "local" and r["llm_score"] is not None for r in test_db.news("NVDA", limit=10))
    res2 = await e.score_with_llm()                         # paused: no second call
    assert res2.get("paused") and len(calls) == 1
    assert len([x for x in test_db.events(limit=50) if "LLM headline scoring failed" in x["message"]]) == 1


def test_validate_llm_rows_accepts_event_and_drops_unknown_event():
    from app.sentiment import validate_llm_rows
    rows = [{"ref": 1, "score": 0.1, "relevance": 1, "label": "neutral", "event": "offering", "reason": ""},
            {"ref": 2, "score": 0.1, "relevance": 1, "label": "neutral", "event": "made_up", "reason": ""}]
    out = validate_llm_rows(rows, {1: 0, 2: 0})
    assert out[0]["event"] == "offering" and out[1]["event"] == "none"


# --- weighting ------------------------------------------------------------------------------

def _item(title, score, hours, source="Reuters", scorer="llm", rel=1.0, **kw):
    return {"title": title, "llm_score": score, "llm_relevance": rel, "scorer": scorer, "source": source,
            "published_at": _iso(hours), "summary": "", **kw}


def test_aggregate_recency_source_and_counts():
    now = _now()
    fresh_vs_old = ne.aggregate("NVDA", [_item("Nvidia a", 0.8, 1), _item("Nvidia b", -0.8, 60)],
                                now=now, lookback_hours=72)
    assert fresh_vs_old["score"] > 0.15                      # 60h-old negative barely counts
    tier = ne.aggregate("NVDA", [_item("Nvidia a", 0.8, 1, "Reuters"), _item("Nvidia b", -0.8, 1, "Insider Monkey")],
                        now=now, lookback_hours=72)
    assert tier["score"] > 0                                  # quality source outweighs the blog
    agg = ne.aggregate("NVDA", [_item("Nvidia a", 0.5, 1), _item("AMD only", 0.9, 1),
                                _item("Nvidia old", 0.5, 100), _item("Nvidia unscored", None, 1)],
                       now=now, lookback_hours=72)
    assert agg["n"] == 1 and agg["pending"] == 1 and agg["n_relevant"] == 2       # irrelevant + stale excluded
    empty = ne.aggregate("NVDA", [], now=now, lookback_hours=72)
    assert empty["score"] is None and empty["n"] == 0
    local = ne.aggregate("NVDA", [_item("Nvidia a", 0.8, 1, scorer="local")], now=now, lookback_hours=72)
    llm = ne.aggregate("NVDA", [_item("Nvidia a", 0.8, 1, scorer="llm")], now=now, lookback_hours=72)
    assert 0 < local["score"] < llm["score"]                  # lexicon scores carry less weight
    assert local["by_scorer"] == {"local": 1}


def test_source_tiers():
    assert ne.source_weight("Reuters") > ne.source_weight("Benzinga") > ne.source_weight("Insider Monkey")
    assert ne.source_weight(None) == 0.5


# --- event classification ---------------------------------------------------------------------

@pytest.mark.parametrize("sym,title,event", [
    ("MU", "Micron downgraded by BNP Paribas Exane with a new price target", "downgrade"),
    ("HPE", "HPE Stock Is Downgraded After Rising 159% This Year", "downgrade"),
    ("IBM", "IBM stock craters 25% after company issues second-quarter earnings warning", "guidance_cut"),
    ("NOW", "ServiceNow cuts full-year guidance", "guidance_cut"),
    ("MU", "Micron prices $2 billion public offering of common stock", "offering"),
    ("SMCI", "Super Micro announces $1.5B convertible senior notes offering", "offering"),
    ("NVDA", "Nvidia Faces an SEC Probe, According to Press Reports", "investigation"),
    ("SMCI", "DOJ opens investigation into Super Micro accounting", "investigation"),
    ("AMD", "AMD hit with class action lawsuit", "investigation"),
    ("MRVL", "FDA issues complete response letter to Acme Bio", "fda_reject"),
    ("COHR", "Coherent files for Chapter 11 bankruptcy", "bankruptcy"),
    ("COHR", "Auditor raises going concern doubt on Coherent", "bankruptcy"),
    ("ARM", "Arm Holdings to be acquired by SoftBank in $50B deal", "acquisition_target"),
    ("AMAT", "Lam Research to acquire Applied Materials for $90B", "acquisition_target"),
    ("LSCC", "Lattice Semiconductor receives unsolicited takeover offer", "acquisition_target"),
    ("INTC", "Intel receives Nasdaq delisting notice", "delisting"),
    ("TXN", "Texas Instruments stocks jump after Cantor upgrade", "upgrade"),
    ("ONTO", "Onto Innovation raises Q4 outlook", "guidance_raise"),
])
def test_classify_event_positives(sym, title, event):
    assert ne.classify_event(title, sym) == event


@pytest.mark.parametrize("sym,title", [
    ("INTC", "Nvidia to buy $5 billion stake in Intel"),                   # investment, not a takeover
    ("INTC", "Intel to acquire SambaNova"),                                 # Intel is the acquirer
    ("AMAT", "Which Gold ETF Is the Better Buy: AMAT"),
    ("AAPL", "Pomerantz Law Firm investigates claims on behalf of Apple investors"),   # law-firm spam
    ("ORCL", "Oracle financing raises debt and dilution risks"),
    ("NVDA", "Nvidia earnings preview: what to expect"),
    ("MU", "Micron IPO of its memory unit"),
])
def test_classify_event_no_false_alarm(sym, title):
    assert ne.classify_event(title, sym) not in ne.NEGATIVE_EVENTS


def test_positive_events_are_not_negative():
    assert not ne.is_negative_event("upgrade") and not ne.is_negative_event(None)
    assert ne.is_negative_event("offering")


# --- eligibility event veto --------------------------------------------------------------------

def _checker(settings, db, **over):
    s = replace(settings, eligibility_enabled=True, eligibility_earnings_blackout_days=0,
                eligibility_max_gap_pct=0, eligibility_first_15_min=False,
                eligibility_one_starter_per_symbol=False, eligibility_min_price=0,
                eligibility_min_volume=0, eligibility_min_bars=0,
                eligibility_signal_freshness_min=0, **over)
    return EligibilityChecker(s, object(), types.SimpleNamespace(sentiment=types.SimpleNamespace(db=db)), db)


def _store(db, news_id, title, hours_ago, symbol="MU", source="Reuters", **fields):
    db.upsert_news({"news_id": news_id, "symbol": symbol, "title": title, "summary": "", "source": source,
                    "url": None, "published_at": _iso(hours_ago), "sentiment": None})
    if fields:
        db.score_news(news_id, symbol, 0.0, 1.0, "neutral", "", **fields)


@pytest.mark.asyncio
async def test_event_veto_blocks_buy_with_clear_reason(test_settings, test_db):
    _store(test_db, "d1", "Micron downgraded by BNP Paribas Exane", 5)
    r = await _checker(test_settings, test_db).check("MU", "BUY", 100.0)
    assert r.ok is False
    assert r.reason.startswith("Event veto: analyst downgrade — Micron downgraded by BNP Paribas Exane (Reuters, 5h ago)")
    assert r.details["event"] == "downgrade"


@pytest.mark.asyncio
async def test_event_veto_scope(test_settings, test_db):
    c = _checker(test_settings, test_db)
    _store(test_db, "old", "Micron prices public offering of common stock", 60)           # outside 48 h
    _store(test_db, "pos", "Micron upgraded by Morgan Stanley", 2)                        # positive: no force, no block
    _store(test_db, "other", "AMD prices public offering of common stock", 1)             # other company
    assert (await c.check("MU", "BUY", 100.0)).ok is True
    assert (await c.check("MU", "SELL", 100.0)).ok is True                                # exits never vetoed
    _store(test_db, "llm", "Micron faces uncertainty", 3, scorer="llm", llm_event="bankruptcy")   # LLM label
    r = await c.check("MU", "BUY", 100.0)
    assert r.ok is False and "bankruptcy / going concern" in r.reason


@pytest.mark.asyncio
async def test_event_veto_toggle_and_window(test_settings, test_db):
    _store(test_db, "d1", "Micron downgraded by BNP Paribas Exane", 5)
    assert (await _checker(test_settings, test_db, event_veto_enabled=False).check("MU", "BUY", 1.0)).ok
    assert (await _checker(test_settings, test_db, event_veto_hours=3.0).check("MU", "BUY", 1.0)).ok
    assert not (await _checker(test_settings, test_db, event_veto_hours=6.0).check("MU", "BUY", 1.0)).ok


# --- earnings dates ----------------------------------------------------------------------------

class _FakeTicker:
    cal = {"Earnings Date": [datetime(2026, 11, 18).date()]}
    hist_index = [datetime(2026, 11, 17, 16, 0), datetime(2027, 2, 24, 16, 0)]
    cal_exc = hist_exc = None

    def __init__(self, sym): pass

    @property
    def calendar(self):
        if self.cal_exc:
            raise self.cal_exc
        return self.cal

    def get_earnings_dates(self, limit=12):
        if self.hist_exc:
            raise self.hist_exc
        return types.SimpleNamespace(index=self.hist_index + [None])


def _fake_yf(monkeypatch, **attrs):
    t = type("T", (_FakeTicker,), attrs)
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=t))


@pytest.mark.asyncio
async def test_earnings_dates_union_cache_and_fail_closed_semantics(monkeypatch):
    _fake_yf(monkeypatch)
    y = YahooFinanceService(None)
    assert await y.get_earnings_dates("nvda") == ["2026-11-17", "2026-11-18", "2027-02-24"]   # stale calendar + history
    _fake_yf(monkeypatch, cal_exc=RuntimeError("x"), hist_exc=RuntimeError("y"))
    assert await y.get_earnings_dates("NVDA") == ["2026-11-17", "2026-11-18", "2027-02-24"]   # served from the 12 h cache
    y2 = YahooFinanceService(None)
    assert await y2.get_earnings_dates("AMD") is None                  # both sources failed -> unreadable
    _fake_yf(monkeypatch, cal=None, hist_exc=RuntimeError("no data"), cal_exc=None, info={"quoteType": "ETF"})
    assert await YahooFinanceService(None).get_earnings_dates("SPYM") == []   # ETF: answered, nothing scheduled
    _fake_yf(monkeypatch, cal_exc=RuntimeError("x"))                    # one source down: the other still answers
    assert await YahooFinanceService(None).get_earnings_dates("MU") == ["2026-11-17", "2027-02-24"]


@pytest.mark.asyncio
async def test_failed_lookup_is_not_cached_as_success(monkeypatch):
    _fake_yf(monkeypatch, cal_exc=RuntimeError("x"), hist_exc=RuntimeError("y"))
    y = YahooFinanceService(None)
    assert await y.get_earnings_dates("MU") is None
    y._negative.clear()
    _fake_yf(monkeypatch)
    assert await y.get_earnings_dates("MU") is not None


@pytest.mark.asyncio
async def test_earnings_blackout_uses_real_dates_and_fails_closed(test_settings, test_db):
    from app.market import now_et
    today = now_et().date()
    base = replace(test_settings, eligibility_enabled=True, eligibility_earnings_blackout_days=3,
                   eligibility_max_gap_pct=0, eligibility_first_15_min=False,
                   eligibility_one_starter_per_symbol=False, eligibility_min_price=0,
                   eligibility_min_volume=0, eligibility_min_bars=0, eligibility_signal_freshness_min=0,
                   event_veto_enabled=False)
    sig = types.SimpleNamespace(sentiment=types.SimpleNamespace(db=test_db))

    def mk(settings, provider):
        c = EligibilityChecker(settings, object(), sig, test_db)
        c.earnings_provider = provider
        return c
    soon = (today + timedelta(days=1)).isoformat()
    far = (today + timedelta(days=30)).isoformat()
    assert not (await mk(base, lambda s: [far, soon]).check("MU", "BUY", 100.0)).ok
    assert (await mk(base, lambda s: [far]).check("MU", "BUY", 100.0)).ok
    live = replace(base, broker_mode="webull")
    r = await mk(live, lambda s: None).check("MU", "BUY", 100.0)
    assert not r.ok and "fails closed" in r.reason                      # live: unreadable calendar blocks
    assert (await mk(base, lambda s: None).check("MU", "BUY", 100.0)).ok  # mock mode skips it
    assert (await mk(live, lambda s: []).check("MU", "BUY", 100.0)).ok  # answered, nothing scheduled
