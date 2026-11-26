"""Activity log and event retention"""

from __future__ import annotations

from ..market import now_utc
from .schema import _now
from .base import BaseRepo


class EventsRepo(BaseRepo):
    """Mixin for the DB facade: activity log and event retention."""

    # -- activity log ------------------------------------------------------
    def log(self, level: str, message: str, alert: bool = False) -> None:
        """Add to the activity feed. alert=True also sends it to your alert channels
        (errors are always sent; see ALERT_LEVELS)."""
        ns_where, ns_args = self._ns_where()
        self.conn.execute(
            f"INSERT INTO events (ts, level, message, broker_mode, webull_environment, account_id) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), level, message, self.broker_mode, self.webull_environment, self.account_id)
        )
        self._events_changed()
        for fn in self.listeners:
            try:
                fn(level, message, alert)
            except Exception:  # noqa: BLE001 - an alert problem must never break logging
                pass


    def events(self, limit: int = 100) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM events WHERE {ns_where} ORDER BY id DESC LIMIT ?", *ns_args, limit)

    # -- retention ---------------------------------------------------------------------
    # table -> its timestamp column (news_items has no `ts`: it is fetched_at)
    PRUNE_TABLES = {"news_items": "fetched_at", "events": "ts", "llm_usage": "ts",
                    "equity_snapshots": "ts"}
    DEFAULT_RETENTION_DAYS = 90

    def prune(self, retention_days: int | None = None) -> dict[str, int]:
        """Delete rows older than ``retention_days`` from news_items, events, llm_usage and
        equity_snapshots (all namespaces). Never touches the ledger, proposals, lessons or
        autopsies. Returns the number of rows removed per table."""
        from datetime import timedelta
        days = self.DEFAULT_RETENTION_DAYS if retention_days is None else int(retention_days)
        cutoff = (now_utc() - timedelta(days=max(0, days))).isoformat(timespec="seconds")
        out: dict[str, int] = {}
        with self.transaction():
            for table, col in self.PRUNE_TABLES.items():
                cur = self.conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                out[table] = cur.rowcount or 0
        return out
