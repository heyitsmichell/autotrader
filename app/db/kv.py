"""Small persistent settings (kill switch, equity peak, ...)"""

from __future__ import annotations

import json
from typing import Any
from .base import BaseRepo


class KvRepo(BaseRepo):
    """Mixin for the DB facade: small persistent settings (kill switch, equity peak, ...)."""

    # -- small persistent settings (kill switch, equity peak, ...) -----------
    def _ns_key(self, key: str) -> str:
        """Return the namespaced key for kv storage."""
        ns = f"{self.broker_mode}:{self.webull_environment or 'none'}:{self.account_id or 'none'}"
        return f"{ns}:{key}"

    def get_kv(self, key: str, default: Any = None) -> Any:
        ns_where, ns_args = self._ns_where_kv()
        ns_key = self._ns_key(key)
        row = self._one(f"SELECT value FROM kv WHERE key=? AND {ns_where}", ns_key, *ns_args)
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_kv(self, key: str, value: Any) -> None:
        ns = f"{self.broker_mode}:{self.webull_environment or 'none'}:{self.account_id or 'none'}"
        ns_key = f"{ns}:{key}"
        self.conn.execute(
            "INSERT INTO kv (key, value, namespace) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, namespace=excluded.namespace",
            (ns_key, json.dumps(value), ns)
        )
