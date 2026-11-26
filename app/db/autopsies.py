"""Trade autopsies"""

from __future__ import annotations

import json

from .schema import _now, decode
from .base import BaseRepo


class AutopsiesRepo(BaseRepo):
    """Mixin for the DB facade: trade autopsies."""

    def add_autopsy(self, trade: dict, causes: list[dict], llm_narration: str | None = None) -> int:
        """Record an autopsy for a closed trade."""
        pnl_pct = trade.get("realized_pnl_pct") or 0
        outcome = "win" if pnl_pct > 0.5 else "loss" if pnl_pct < -0.5 else "breakeven"

        # Classify process based on causes
        bad_process = any(c.get("category") == "process" for c in causes)
        good_process = any(c.get("category") == "good_process" for c in causes)
        if bad_process and not good_process:
            process_class = "bad"
        elif good_process and not bad_process:
            process_class = "good"
        else:
            process_class = "mixed"

        fields = {
            "trade_id": trade["id"],
            "proposal_id": trade["proposal_id"],
            "symbol": trade["symbol"],
            "side": trade["side"],
            "outcome_class": outcome,
            "process_class": process_class,
            "causes": json.dumps(causes),
            "pnl_pct": pnl_pct,
            "mae_pct": trade.get("mae"),
            "mfe_pct": trade.get("mfe"),
            "hold_hours": (trade.get("hold_seconds") or 0) / 3600.0,
            "llm_narration": llm_narration,
            "created_at": _now(),
            "broker_mode": self.broker_mode,
            "webull_environment": self.webull_environment,
            "account_id": self.account_id,
        }
        return self._insert("autopsies", fields)


    def autopsies(self, limit: int = 50, symbol: str | None = None) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        where = [ns_where]
        args = list(ns_args)
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        sql = f"SELECT * FROM autopsies WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
        rows = self._rows(sql, *args, limit)
        return [decode(r) for r in rows]

    def get_autopsy(self, trade_id: int) -> dict | None:
        ns_where, ns_args = self._ns_where()
        row = self._one(f"SELECT * FROM autopsies WHERE trade_id=? AND {ns_where}", trade_id, *ns_args)
        return decode(row)

    def autopsies_needing_llm(self, limit: int = 10) -> list[dict]:
        """Autopsies that need LLM narration."""
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM autopsies WHERE llm_narration IS NULL AND {ns_where} ORDER BY id DESC LIMIT ?",
            *ns_args, limit)

    def update_autopsy_narration(self, autopsy_id: int, narration: str) -> None:
        ns_where, ns_args = self._ns_where()
        self.conn.execute(f"UPDATE autopsies SET llm_narration=? WHERE id=? AND {ns_where}",
                          (narration, autopsy_id, *ns_args))
