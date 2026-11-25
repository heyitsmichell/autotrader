"""Proposals and their outcome tracking"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..market import start_of_today_et_utc
from .base import BaseRepo
from .schema import _now


class DuplicateProposalError(ValueError):
    """An open (pending/submitting) proposal for this symbol + side already exists."""

    def __init__(self, symbol: str | None = None, side: str | None = None) -> None:
        self.symbol = symbol
        self.side = side
        super().__init__(f"open proposal already exists for {symbol} {side}")


def _is_dup(err: sqlite3.IntegrityError) -> bool:
    return "ux_proposals_open" in str(err) or "UNIQUE constraint failed" in str(err)


class ProposalsRepo(BaseRepo):
    """Mixin for the DB facade: proposals and their outcome tracking."""

    # -- proposals ---------------------------------------------------------
    def add_proposal(self, run_id: int | None, p: dict, status: str, reason: str = "",
                     ref_price: float | None = None, features: str | None = None,
                     **extra: Any) -> int:
        fields = {
            "run_id": run_id, "created_at": _now(), "symbol": p["symbol"], "side": p["side"],
            "quantity": p["quantity"], "limit_price": p["limit_price"],
            "confidence": p.get("confidence"), "rationale": p.get("rationale"),
            "ref_price": ref_price, "status": status, "reason": reason, "features": features,
            "broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id,
        }
        for k, v in extra.items():
            fields[k] = json.dumps(v) if isinstance(v, (list, dict)) else v
        try:
            pid = self._insert("proposals", fields)
        except sqlite3.IntegrityError as e:
            if _is_dup(e):
                raise DuplicateProposalError(p["symbol"], p["side"]) from e
            raise
        self._proposals_changed()
        return pid

    def update_proposal(self, pid: int, **fields: Any) -> None:
        ns_where, ns_args = self._ns_where()
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            try:
                self.conn.execute(f"UPDATE proposals SET {cols} WHERE id=? AND {ns_where}",
                                  (*fields.values(), pid, *ns_args))
            except sqlite3.IntegrityError as e:
                if _is_dup(e):
                    raise DuplicateProposalError() from e
                raise
            self._proposals_changed()

    def claim_pending(self, pid: int) -> bool:
        """Atomically move a proposal from pending to submitting."""
        ns_where, ns_args = self._ns_where()
        try:
            cur = self.conn.execute(
                f"UPDATE proposals SET status='submitting' WHERE id=? AND status='pending' AND {ns_where}", (pid, *ns_args)
            )
        except sqlite3.IntegrityError as e:
            if _is_dup(e):          # another submitting proposal for the same symbol + side
                return False
            raise
        if cur.rowcount == 1:
            self._proposals_changed()
        return cur.rowcount == 1

    def get_proposal(self, pid: int) -> dict | None:
        ns_where, ns_args = self._ns_where()
        return self._one(f"SELECT * FROM proposals WHERE id=? AND {ns_where}", pid, *ns_args)

    def proposals(self, limit: int = 50, symbol: str | None = None) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        if symbol:
            return self._rows(f"SELECT * FROM proposals WHERE symbol=? AND {ns_where} ORDER BY id DESC LIMIT ?", symbol, *ns_args, limit)
        return self._rows(f"SELECT * FROM proposals WHERE {ns_where} ORDER BY id DESC LIMIT ?", *ns_args, limit)

    def pending_proposals(self) -> list[dict]:
        """All proposals still waiting for a decision (called by the engine's deterministic cycle)."""
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM proposals WHERE status='pending' AND {ns_where} ORDER BY id", *ns_args)

    def pending_for_run(self, run_id: int) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM proposals WHERE run_id=? AND status='pending' AND {ns_where} ORDER BY id", run_id, *ns_args
        )

    def count_for_run(self, run_id: int) -> int:
        ns_where, ns_args = self._ns_where()
        return self.conn.execute(
            f"SELECT COUNT(*) FROM proposals WHERE run_id=? AND {ns_where}", (run_id, *ns_args)
        ).fetchone()[0]

    def count_placed_today(self, asset_class: str | None = None) -> int:
        """Orders placed today. ``asset_class`` "equity" counts stock orders only (option orders
        have their own cap, OPTIONS_ENTRY_MAX_PER_DAY); "option" counts option OPENING orders."""
        ns_where, ns_args = self._ns_where()
        since = start_of_today_et_utc().isoformat(timespec="seconds")
        extra = ""
        if asset_class == "equity":
            extra = "AND asset_class != 'option' "
        elif asset_class == "option":
            extra = "AND asset_class = 'option' AND side = 'BUY' "
        return self.conn.execute(
            f"SELECT COUNT(*) FROM proposals WHERE status IN ('placed','unknown','submitting') "
            f"{extra}AND COALESCE(placed_at, created_at) >= ? AND {ns_where}",
            (since, *ns_args),
        ).fetchone()[0]

    def unresolved_proposals(self) -> list[dict]:
        """Orders whose fate at the broker is not known yet (timeouts, or the app
        stopped while sending)."""
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM proposals WHERE status IN ('unknown','submitting') AND {ns_where} ORDER BY id", *ns_args)

    def dismiss_all_pending(self, reason: str) -> int:
        ns_where, ns_args = self._ns_where()
        cur = self.conn.execute(
            f"UPDATE proposals SET status='dismissed', reason=? WHERE status='pending' AND {ns_where}", (reason, *ns_args)
        )
        if cur.rowcount:
            self._proposals_changed()
        return cur.rowcount

    # -- learning ------------------------------------------------------------
    def proposals_needing_outcomes(self, older_than: str, newer_than: str) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM proposals WHERE ret_5d IS NULL AND created_at < ? AND created_at > ? AND {ns_where} ORDER BY id", older_than, newer_than, *ns_args)

    def evaluated(self, limit: int = 200) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM proposals WHERE ret_5d IS NOT NULL AND {ns_where} ORDER BY id DESC LIMIT ?", *ns_args, limit)

    # -- engine support (additive) ------------------------------------------------------
    def open_proposals(self, symbol: str | None = None, side: str | None = None) -> list[dict]:
        """Rows that hold the partial unique index slot: status pending or submitting."""
        ns_where, ns_args = self._ns_where()
        where, args = ["status IN ('pending','submitting')", ns_where], list(ns_args)
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        if side:
            where.append("side=?")
            args.append(side)
        return self._rows(f"SELECT * FROM proposals WHERE {' AND '.join(where)} ORDER BY id", *args)

    def known_client_order_ids(self) -> set[str]:
        """Every client_order_id this app has ever recorded (proposals and protective orders)."""
        ns_where, ns_args = self._ns_where()
        out: set[str] = set()
        for table in ("proposals", "protective_orders"):
            rows = self._rows(
                f"SELECT client_order_id FROM {table} WHERE client_order_id IS NOT NULL "
                f"AND {ns_where}", *ns_args)
            out.update(r["client_order_id"] for r in rows)
        return out
