"""Tests for parsers (balance, news, fear & greed)."""

import pytest


class TestParsers:
    """Test data parsers."""

    @pytest.mark.asyncio
    async def test_balance_parsed(self, mock_broker):
        """Balance should be parsed correctly."""
        from app.portfolio import parse_balance
        bal = parse_balance(await mock_broker.balance_text())
        assert bal.get("net_liq", 0) > 0
        assert bal.get("cash") is not None

    @pytest.mark.asyncio
    async def test_news_parsed(self, mock_broker):
        """News should be parsed correctly."""
        from app.sentiment import parse_news
        news = parse_news(await mock_broker.call("get_stock_news", symbol="AAPL"), "AAPL")
        assert len(news) > 0
        assert all(n["title"] and n["news_id"] for n in news)

    @pytest.mark.asyncio
    async def test_news_carries_webull_sentiment_tags(self, mock_broker):
        """News should carry Webull sentiment tags."""
        from app.sentiment import parse_news
        news = parse_news(await mock_broker.call("get_stock_news", symbol="AAPL"), "AAPL")
        assert any(n["sentiment"] in ("POSITIVE", "NEGATIVE", "NEUTRAL") for n in news)

    @pytest.mark.asyncio
    async def test_fear_greed_parsed(self, mock_broker):
        """Fear & Greed index should be parsed."""
        from app.sentiment import parse_fear_greed
        fg = parse_fear_greed(await mock_broker.call("get_market_sentiment"))
        assert fg is not None
        assert 0 <= fg["current"]["index"] <= 100

    def test_rss_news_parsed(self):
        """RSS fallback strips HTML, keeps source/URL and parses pubDate."""
        from app.sentiment import parse_rss_news
        feed = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><title>t</title>
          <item>
            <title>Apple beats estimates - Reuters</title>
            <link>https://example.com/a</link>
            <pubDate>Mon, 23 Nov 2026 12:00:00 GMT</pubDate>
            <source url="https://reuters.com">Reuters</source>
            <description>&lt;p&gt;Strong &amp;amp; steady demand&lt;/p&gt;</description>
          </item>
          <item><title></title></item>
        </channel></rss>"""
        items = parse_rss_news(feed, "AAPL")
        assert len(items) == 1
        item = items[0]
        assert item["symbol"] == "AAPL"
        assert item["title"] == "Apple beats estimates - Reuters"
        assert item["source"] == "Reuters"
        assert item["url"] == "https://example.com/a"
        assert item["summary"] == "Strong & steady demand"
        assert item["published_at"].startswith("2026-11-23T12:00:00")
        assert item["sentiment"] is None and item["news_id"]