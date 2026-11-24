"""Research packets"""

from __future__ import annotations

import json

from .schema import _now, decode
from .base import BaseRepo


class ResearchRepo(BaseRepo):
    """Mixin for the DB facade: research packets."""

    # -- research packets ---------------------------------------------------------
    def add_research_packet(self, *, asset_class: str, symbol: str, packet_type: str,
                            evidence: list[dict], caveats: list[str], score: float | None = None,
                            approval_only: bool = True) -> int:
        return self._insert("research_packets", {
            "created_at": _now(), "asset_class": asset_class, "symbol": symbol,
            "packet_type": packet_type, "score": score,
            "evidence": json.dumps(evidence), "caveats": json.dumps(caveats),
            "approval_only": int(approval_only),
            "broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id,
        })

    def research_packets(self, limit: int = 100) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        rows = self._rows(f"SELECT * FROM research_packets WHERE {ns_where} ORDER BY id DESC LIMIT ?", *ns_args, limit)
        return [decode(r) for r in rows]

    def clear_research_packets(self) -> None:
        ns_where, ns_args = self._ns_where()
        self.conn.execute(f"DELETE FROM research_packets WHERE {ns_where}", ns_args)
