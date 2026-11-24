"""ML models, champion/challenger tracking and LLM usage"""

from __future__ import annotations

import json
from typing import Any

from .schema import _now
from .base import BaseRepo


class MLRepo(BaseRepo):
    """Mixin for the DB facade: ml models, champion/challenger tracking and llm usage."""

    # -- models --------------------------------------------------------------------
    def add_model(self, **fields: Any) -> int:
        fields = {k: json.dumps(v) if isinstance(v, (list, dict)) else v
                  for k, v in fields.items()}
        fields.update({"broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id})
        return self._insert("models", {"created_at": _now(), **fields})

    def models(self, limit: int = 20) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM models WHERE {ns_where} ORDER BY id DESC LIMIT ?", *ns_args, limit)

    def active_model(self) -> dict | None:
        """Get the currently active model (champion). Backward compatible with 'active' status."""
        ns_where, ns_args = self._ns_where()
        # First try champion (new), then active (legacy)
        row = self._one(f"SELECT * FROM models WHERE status='champion' AND {ns_where} ORDER BY id DESC LIMIT 1", *ns_args)
        if row:
            return row
        return self._one(f"SELECT * FROM models WHERE status='active' AND {ns_where} ORDER BY id DESC LIMIT 1", *ns_args)

    def set_model_status(self, mid: int, status: str) -> None:
        ns_where, ns_args = self._ns_where()
        self.conn.execute(f"UPDATE models SET status=? WHERE id=? AND {ns_where}", (status, mid, *ns_args))

    def retire_active_models(self) -> None:
        ns_where, ns_args = self._ns_where()
        self.conn.execute(f"UPDATE models SET status='retired' WHERE status IN ('active','champion') AND {ns_where}", ns_args)

    # -- champion/challenger ------------------------------------------------------
    def champion_model(self) -> dict | None:
        """Get the current champion model (the one actively used for trading)."""
        ns_where, ns_args = self._ns_where()
        return self._one(f"SELECT * FROM models WHERE status='champion' AND {ns_where} ORDER BY id DESC LIMIT 1", *ns_args)

    def challenger_model(self) -> dict | None:
        """Get the current challenger model (in shadow evaluation)."""
        ns_where, ns_args = self._ns_where()
        return self._one(f"SELECT * FROM models WHERE status='challenger' AND {ns_where} ORDER BY id DESC LIMIT 1", *ns_args)

    def promote_challenger(self, mid: int, replaced_id: int | None = None) -> None:
        """Promote a challenger to champion, retiring the old champion."""
        ns_where, ns_args = self._ns_where()
        # Retire current champion
        self.conn.execute(f"UPDATE models SET status='retired' WHERE status='champion' AND {ns_where}", ns_args)
        # Promote challenger
        fields = {"status": "champion"}
        if replaced_id:
            fields["champion_replaced_id"] = replaced_id
        self._update("models", mid, fields)

    def reject_challenger(self, mid: int) -> None:
        """Reject a challenger model."""
        self.set_model_status(mid, "rejected")

    def log_challenger_outcome(self, mid: int, hit: bool, return_pct: float) -> None:
        """Log a trade outcome for the challenger's shadow evaluation."""
        challenger = self._one("SELECT shadow_trades, shadow_hit_rate, shadow_avg_return FROM models WHERE id=?", mid)
        if not challenger:
            return
        trades = (challenger["shadow_trades"] or 0) + 1
        old_hit = challenger["shadow_hit_rate"] or 0.0
        old_ret = challenger["shadow_avg_return"] or 0.0
        # Incremental mean update
        new_hit = (old_hit * (trades - 1) + (1.0 if hit else 0.0)) / trades
        new_ret = (old_ret * (trades - 1) + return_pct) / trades
        self._update("models", mid, {
            "shadow_trades": trades,
            "shadow_hit_rate": new_hit,
            "shadow_avg_return": new_ret,
        })

    def get_model_by_id(self, mid: int) -> dict | None:
        """Get a model by ID."""
        ns_where, ns_args = self._ns_where()
        return self._one(f"SELECT * FROM models WHERE id=? AND {ns_where}", mid, *ns_args)

    def rollback_to_champion(self, champion_id: int) -> None:
        """Rollback: retire current champion, restore previous champion."""
        ns_where, ns_args = self._ns_where()
        # Retire current champion
        self.conn.execute(f"UPDATE models SET status='retired' WHERE status='champion' AND {ns_where}", ns_args)
        # Restore the previous champion
        self._update("models", champion_id, {"status": "champion"})
        # Reject any challenger
        self.conn.execute(f"UPDATE models SET status='rejected' WHERE status='challenger' AND {ns_where}", ns_args)

    # -- LLM usage ------------------------------------------------------------------
    def add_usage(self, purpose: str, model: str | None, input_tokens: int, output_tokens: int,
                  cache_read: int = 0, cache_write: int = 0, cost_usd: float | None = None) -> None:
        self._insert("llm_usage", {
            "ts": _now(), "purpose": purpose, "model": model, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cache_read": cache_read, "cache_write": cache_write,
            "cost_usd": cost_usd,
            "broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id})

    def usage_since(self, since: str) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT purpose, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
            f"SUM(output_tokens) AS output_tokens, SUM(COALESCE(cost_usd, 0)) AS cost_usd "
            f"FROM llm_usage WHERE ts >= ? AND {ns_where} GROUP BY purpose", since, *ns_args)
