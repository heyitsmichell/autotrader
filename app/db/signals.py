"""Signals and news"""

from __future__ import annotations

import json
from typing import Any

from .schema import _now
from .base import BaseRepo


class SignalsRepo(BaseRepo):
    """Mixin for the DB facade: signals and news."""

    # -- signals -------------------------------------------------------------
    def add_signal(self, run_id: int | None, s: dict) -> int:
        fields = {"run_id": run_id, "created_at": _now(),
                  "broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id}
        for k, v in s.items():
            fields[k] = json.dumps(v) if isinstance(v, (list, dict)) else v
        return self._insert("signals", fields)

    def get_signal(self, sid: int) -> dict | None:
        ns_where, ns_args = self._ns_where()
        return self._one(f"SELECT * FROM signals WHERE id=? AND {ns_where}", sid, *ns_args)

    def latest_signals(self) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM signals WHERE id IN (SELECT MAX(id) FROM signals WHERE {ns_where} GROUP BY symbol) AND {ns_where} "
            "ORDER BY symbol", *ns_args, *ns_args)

    def signals(self, symbol: str, limit: int = 100) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM signals WHERE symbol=? AND {ns_where} ORDER BY id DESC LIMIT ?", symbol, *ns_args, limit)

    def signals_needing_outcomes(self, older_than: str, newer_than: str) -> list[dict]:
        """The last signal of each symbol per day (one row per symbol-day)."""
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM signals WHERE id IN (SELECT MAX(id) FROM signals "
            f"WHERE created_at < ? AND created_at > ? AND {ns_where} GROUP BY symbol, substr(created_at, 1, 10)) "
            f"AND ret_5d IS NULL AND {ns_where} ORDER BY id", older_than, newer_than, *ns_args, *ns_args)

    def update_signal(self, sid: int, **fields: Any) -> None:
        ns_where, ns_args = self._ns_where()
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE signals SET {cols} WHERE id=? AND {ns_where}", (*fields.values(), sid, *ns_args))

    def evaluated_signals(self, limit: int = 1000) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM signals WHERE ret_5d IS NOT NULL AND {ns_where} ORDER BY id DESC LIMIT ?", *ns_args, limit)

    # -- news -----------------------------------------------------------------
    def upsert_news(self, item: dict) -> bool:
        """Insert a headline; Webull fields refresh and model scores stay."""
        ns_where, ns_args = self._ns_where()
        known = self.conn.execute(f"SELECT 1 FROM news_items WHERE news_id=? AND symbol=? AND {ns_where}",
                                  (item["news_id"], item["symbol"], *ns_args)).fetchone()
        self.conn.execute(
            f"INSERT INTO news_items (news_id, symbol, published_at, title, summary, source, url, "
            f"webull_sentiment, fetched_at, broker_mode, webull_environment, account_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            f"ON CONFLICT(news_id, symbol) DO UPDATE SET webull_sentiment=excluded.webull_sentiment",
            (item["news_id"], item["symbol"], item.get("published_at"), item.get("title"),
             item.get("summary"), item.get("source"), item.get("url"), item.get("sentiment"),
             _now(), self.broker_mode, self.webull_environment, self.account_id))
        return known is None

    def news(self, symbol: str | None = None, since: str | None = None,
             limit: int = 50, until: str | None = None) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        where, args = [ns_where], ns_args
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        if since:
            where.append("COALESCE(published_at, fetched_at) >= ?")
            args.append(since)
        if until:
            where.append("COALESCE(published_at, fetched_at) <= ?")
            args.append(until)
        sql = "SELECT * FROM news_items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?"
        return self._rows(sql, *args, limit)

    def unscored_news(self, since: str, limit: int = 60) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM news_items WHERE scored_at IS NULL AND "
            f"COALESCE(published_at, fetched_at) >= ? AND {ns_where} "
            f"ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?", since, *ns_args, limit)

    def locally_scored_news(self, since: str, limit: int = 60) -> list[dict]:
        """Headlines only the local lexicon has rated so far: candidates for an LLM upgrade."""
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM news_items WHERE scorer='local' AND "
            f"COALESCE(published_at, fetched_at) >= ? AND {ns_where} "
            f"ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?", since, *ns_args, limit)

    def score_news(self, news_id: str, symbol: str, score: float, relevance: float,
                   label: str, reason: str, scorer: str = "llm",
                   event_type: str | None = None, llm_event: str | None = None) -> None:
        """Store a score. ``event_type`` (keyword rule) is only written when given; ``llm_event``
        likewise, so an LLM re-score never erases the keyword event."""
        ns_where, ns_args = self._ns_where()
        sets = ["llm_score=?", "llm_relevance=?", "llm_label=?", "llm_reason=?", "scored_at=?", "scorer=?"]
        vals: list = [score, relevance, label, reason, _now(), scorer]
        if event_type is not None:
            sets.append("event_type=?")
            vals.append(event_type)
        if llm_event is not None:
            sets.append("llm_event=?")
            vals.append(llm_event)
        self.conn.execute(
            f"UPDATE news_items SET {', '.join(sets)} WHERE news_id=? AND symbol=? AND {ns_where}",
            (*vals, news_id, symbol, *ns_args))
