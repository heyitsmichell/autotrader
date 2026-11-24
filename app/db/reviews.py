"""Learning-loop reviews"""

from __future__ import annotations

from typing import Any

from .schema import _now
from .base import BaseRepo


class ReviewsRepo(BaseRepo):
    """Mixin for the DB facade: learning-loop reviews."""

    def add_review(self, trigger: str, status: str, **fields: Any) -> int:
        fields.update({
            "trigger": trigger, "status": status,
            "created_at": _now(),
            "broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id
        })
        return self._insert("reviews", fields)

    def reviews(self, limit: int = 5) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM reviews WHERE {ns_where} ORDER BY id DESC LIMIT ?", *ns_args, limit)

    def last_ok_review(self) -> dict | None:
        ns_where, ns_args = self._ns_where()
        return self._one(f"SELECT * FROM reviews WHERE status='ok' AND {ns_where} ORDER BY id DESC LIMIT 1", *ns_args)
