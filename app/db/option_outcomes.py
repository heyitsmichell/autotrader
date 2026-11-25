"""Closed option trades kept for the options ML (option_outcomes)."""

from __future__ import annotations

import json

from .base import BaseRepo
from .schema import _now


class OptionOutcomesRepo(BaseRepo):
    def add_option_outcome(self, row: dict) -> None:
        """Insert (once) the outcome of the BUY proposal ``row['proposal_id']``."""
        cols = ("proposal_id", "symbol", "underlying", "entry_premium", "exit_premium", "ret",
                "exit_reason", "opened_at", "closed_at", "selection_json", "ml_p", "explore")
        vals = [row.get(c) for c in cols] + [_now()]
        self.conn.execute(
            f"INSERT OR IGNORE INTO option_outcomes ({','.join(cols)},recorded_at) "
            f"VALUES ({','.join('?' * (len(cols) + 1))})", vals)

    def option_outcomes(self, limit: int = 1000) -> list[dict]:
        rows = self._rows("SELECT * FROM option_outcomes ORDER BY closed_at DESC LIMIT ?", int(limit))
        for r in rows:
            try:
                r["selection"] = json.loads(r.get("selection_json") or "null") or {}
            except ValueError:
                r["selection"] = {}
        return rows

    def has_option_outcome(self, proposal_id: int) -> bool:
        return self._one("SELECT 1 AS x FROM option_outcomes WHERE proposal_id=?", proposal_id) is not None
