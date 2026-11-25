"""New RSS sources (Nasdaq, Seeking Alpha, Bing), Google gating/spacing, outage alert. No network."""
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from app.broker import BrokerError
from app.sentiment import SentimentEngine


def _now():
    from app.market import now_utc
    return now_utc()


def _rss(titles, pub="Wire", hours=1):
    from email.utils import format_datetime
    d = format_datetime(_now() - timedelta(hours=hours))
    body = "".join(f"<item><title>{t}</title><link>https://x/{i}</link><pubDate>{d}</pubDate>"
                   + (f"<source>{pub}</source>" if pub else "") + "</item>" for i, t in enumerate(titles))
    return f"<rss><channel>{body}</channel></rss>"


class DeadBroker:
    async def call(self, name, **kw):
        raise BrokerError("down")


def _engine(settings, db, **over):
    base = dict(sentiment_rss_fallback=True, sentiment_yahoo_rss=False, news_refresh_minutes=0,
                sentiment_llm=False, news_rss_spacing_seconds=0.0, news_google_spacing_seconds=0.0,
                news_webull_enabled=False, sentiment_source="blend", news_google_refresh_minutes=30)
    return SentimentEngine(replace(settings, **{**base, **over}), DeadBroker(), db)


def _router(calls, feeds=None):
    feeds = feeds if feeds is not None else {}

    async def fake(url):
        calls.append(url)
        for host, text in feeds.items():
            if host in url:
                return text
        return _rss([])
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("host,pub,label", [
    ("nasdaq.com", "Nasdaq", "nasdaq_rss"),
    ("seekingalpha.com", "Seeking Alpha", "seekingalpha_rss"),
    ("bing.com", "Bing News", "bing_rss"),
])
async def test_new_source_parsed_with_default_publisher(test_settings, test_db, host, pub, label):
    e = _engine(test_settings, test_db, sentiment_rss_fallback=False)
    calls = []
    e._http_text = _router(calls, {host: _rss(["Nvidia beats estimates"], pub=None)})
    await e.refresh(["NVDA"])
    rows = test_db.news("NVDA", since=e._since(), limit=20)
    assert len(rows) == 1 and rows[0]["source"] == pub


@pytest.mark.asyncio
async def test_bing_query_has_no_when_and_urls(test_settings, test_db):
    e = _engine(test_settings, test_db)
    calls = []
    e._http_text = _router(calls)
    await e.refresh(["NVDA"])
    bing = [u for u in calls if "bing.com" in u]
    assert len(bing) == 1 and "when" not in bing[0] and "%22NVDA%22+stock" in bing[0]
    assert any("symbol=NVDA" in u for u in calls if "nasdaq.com" in u)
    assert any(u.endswith("/NVDA.xml") for u in calls)


@pytest.mark.asyncio
async def test_disabled_sources_not_called(test_settings, test_db):
    e = _engine(test_settings, test_db, news_nasdaq_enabled=False, news_seekingalpha_enabled=False,
                news_bing_enabled=False, sentiment_rss_fallback=False)
    calls = []
    e._http_text = _router(calls)
    await e.refresh(["NVDA"])
    assert calls == []


@pytest.mark.asyncio
async def test_google_refresh_gating(test_settings, test_db, monkeypatch):
    e = _engine(test_settings, test_db, news_nasdaq_enabled=False, news_seekingalpha_enabled=False,
                news_bing_enabled=False)
    calls = []
    e._http_text = _router(calls)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    await e.refresh(["NVDA"])
    clock[0] += 600
    await e.refresh(["NVDA"])
    assert len(calls) == 1
    clock[0] += 31 * 60 - 600
    await e.refresh(["NVDA"])
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_google_spacing_setting_used(test_settings, test_db):
    e = _engine(test_settings, test_db, news_google_spacing_seconds=5.0, news_rss_spacing_seconds=0.0)
    await e._pace("google")
    assert e._rss_next["google"] - time.monotonic() > 4
    await e._pace("bing")
    assert "bing" not in e._rss_next


@pytest.mark.asyncio
async def test_outage_alert_once_and_recovery(test_settings, test_db, monkeypatch):
    e = _engine(test_settings, test_db)
    calls = []
    feeds = {}
    e._http_text = _router(calls, feeds)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    e._last_news_ok = clock[0]
    await e.refresh(["NVDA"])                     # fresh boot: no alert
    assert not [x for x in test_db.events(limit=50) if "News is DOWN" in x["message"]]
    for _ in range(3):
        clock[0] += 61 * 60
        await e.refresh(["NVDA"])
    down = [x for x in test_db.events(limit=50) if "News is DOWN" in x["message"]]
    assert len(down) == 1 and down[0]["level"] == "error"
    feeds["bing.com"] = _rss(["Nvidia beats estimates"])
    clock[0] += 60
    await e.refresh(["NVDA"])
    back = [x for x in test_db.events(limit=50) if "News is back" in x["message"]]
    assert len(back) == 1 and "bing" in back[0]["message"]
