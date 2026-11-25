"""Trade ledger"""

from __future__ import annotations

import json

from .schema import _now
from .base import BaseRepo



def features_json(features) -> str | None:
    """Entry features as JSON text, encoded ONCE. Signal rows can already carry them as JSON text;
    json.dumps on that double-encoded every ledger row (2026-11-25: training failed with
    "'str' object has no attribute 'get'")."""
    if features is None:
        return None
    if isinstance(features, str):
        try:
            decoded = json.loads(features)
        except ValueError:
            return None
        return json.dumps(decoded) if isinstance(decoded, dict) else None
    return json.dumps(features)

class LedgerRepo(BaseRepo):
    """Mixin for the DB facade: trade ledger."""

    def add_trade_entry(self, proposal_id: int, p: dict, sig: dict | None = None) -> int:
        """Record a trade entry when a proposal is placed (or a bracket is sent)."""
        is_frac = isinstance(p.get("quantity"), float) and p["quantity"] != int(p["quantity"])
        fields = {
            "proposal_id": proposal_id,
            "symbol": p["symbol"],
            "side": p["side"],
            "quantity_requested": int(p["quantity"]) if not is_frac else p["quantity"],
            "quantity_filled": 0,
            "limit_price": p["limit_price"],
            "stop_price": p.get("sl"),
            "target_price": p.get("tp"),
            "entry_price": p.get("ref_price") or p.get("limit_price"),
            "entry_atr": sig.get("atr") if sig else None,
            "entry_trend": sig.get("trend") if sig else None,
            "entry_ta_score": sig.get("ta_score") if sig else None,
            "entry_sent_score": sig.get("sent_score") if sig else None,
            "entry_ml_p": sig.get("ml_p") if sig else None,
            "entry_features": features_json(sig.get("features")) if sig and sig.get("features") else None,
            "status": "open",
            "created_at": _now(),
            "updated_at": _now(),
            "broker_mode": self.broker_mode,
            "webull_environment": self.webull_environment,
            "account_id": self.account_id,
            "quantity_requested_real": p["quantity"],
            "quantity_filled_real": 0.0,
            "entrust_type": p.get("entrust_type", "QTY"),
            "total_cash_amount": p.get("total_cash_amount"),
        }
        return self._insert("trade_ledger", fields)

    def update_trade_fill(self, proposal_id: int, fill_price: float, fill_time: str,
                          quantity_filled: float, commission: float = 0.0,
                          slippage: float | None = None) -> None:
        """Update trade ledger when entry fills."""
        ns_where, ns_args = self._ns_where()
        fields = {
            "entry_fill_price": fill_price,
            "entry_fill_time": fill_time,
            "quantity_filled": int(quantity_filled),
            "quantity_filled_real": quantity_filled,
            "entry_commission": commission,
            "entry_slippage": slippage,
            "updated_at": _now(),
        }
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE trade_ledger SET {cols} WHERE proposal_id=? AND {ns_where}",
                          (*fields.values(), proposal_id, *ns_args))

    def update_trade_exit(self, proposal_id: int, exit_price: float, exit_time: str,
                          exit_reason: str, commission: float = 0.0,
                          slippage: float | None = None, quantity_filled: float | None = None) -> None:
        """Update trade ledger when position exits."""
        ns_where, ns_args = self._ns_where()
        fields = {
            "exit_fill_price": exit_price,
            "exit_fill_time": exit_time,
            "exit_reason": exit_reason,
            "exit_commission": commission,
            "exit_slippage": slippage,
            "status": "closed",
            "updated_at": _now(),
        }
        if quantity_filled is not None:
            fields["quantity_filled_real"] = quantity_filled
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE trade_ledger SET {cols} WHERE proposal_id=? AND {ns_where}",
                          (*fields.values(), proposal_id, *ns_args))

    def update_trade_pnl(self, proposal_id: int, realized_pnl: float, realized_pnl_pct: float,
                         mae: float, mfe: float, hold_seconds: int) -> None:
        """Update P&L, MAE, MFE for a closed trade."""
        ns_where, ns_args = self._ns_where()
        fields = {
            "realized_pnl": realized_pnl,
            "realized_pnl_pct": realized_pnl_pct,
            "mae": mae,
            "mfe": mfe,
            "hold_seconds": hold_seconds,
            "updated_at": _now(),
        }
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE trade_ledger SET {cols} WHERE proposal_id=? AND {ns_where}",
                          (*fields.values(), proposal_id, *ns_args))

    def get_trade(self, proposal_id: int) -> dict | None:
        ns_where, ns_args = self._ns_where()
        return self._one(f"SELECT * FROM trade_ledger WHERE proposal_id=? AND {ns_where}", proposal_id, *ns_args)

    def trades(self, limit: int = 100, symbol: str | None = None, status: str | None = None) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        where = [ns_where]
        args = list(ns_args)
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        if status:
            where.append("status=?")
            args.append(status)
        sql = f"SELECT * FROM trade_ledger WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
        return self._rows(sql, *args, limit)

    def cancel_trade(self, proposal_id: int, reason: str) -> None:
        """Close a ledger row whose entry order ended without filling (expired, cancelled,
        rejected, or never found at the broker). It is not a trade: it leaves open_trades()
        so it cannot skew time stops, position counts or ML/lesson data."""
        ns_where, ns_args = self._ns_where()
        self.conn.execute(
            f"UPDATE trade_ledger SET status='cancelled', exit_reason=?, updated_at=? "
            f"WHERE proposal_id=? AND status='open' AND entry_fill_price IS NULL AND {ns_where}",
            (reason, _now(), proposal_id, *ns_args))

    def open_trades(self) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM trade_ledger WHERE status='open' AND {ns_where} ORDER BY id DESC", *ns_args)

    def closed_trades(self, limit: int = 100) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM trade_ledger WHERE status='closed' AND {ns_where} ORDER BY id DESC LIMIT ?", *ns_args, limit)

    def trades_needing_outcomes(self, older_than: str, newer_than: str) -> list[dict]:
        """Trades that are closed but need P&L/MAE/MFE calculated."""
        ns_where, ns_args = self._ns_where()
        return self._rows(
            f"SELECT * FROM trade_ledger WHERE status='closed' AND realized_pnl IS NULL "
            f"AND exit_fill_time < ? AND exit_fill_time > ? AND {ns_where} ORDER BY id",
            older_than, newer_than, *ns_args)

    def update_trade_levels(self, proposal_id: int, stop_price: float | None,
                            target_price: float | None) -> None:
        """The stop/target were re-anchored from the actual fill (it landed below the plan)."""
        ns_where, ns_args = self._ns_where()
        self.conn.execute(
            f"UPDATE trade_ledger SET stop_price=?, target_price=?, updated_at=? "
            f"WHERE proposal_id=? AND {ns_where}",
            (stop_price, target_price, _now(), proposal_id, *ns_args))

    def update_trade_limit(self, proposal_id: int, limit_price: float) -> None:
        """The entry was re-sent at a new limit (entry chase): slippage is measured from it."""
        ns_where, ns_args = self._ns_where()
        self.conn.execute(
            f"UPDATE trade_ledger SET limit_price=?, updated_at=? "
            f"WHERE proposal_id=? AND entry_fill_price IS NULL AND {ns_where}",
            (float(limit_price), _now(), proposal_id, *ns_args))

    def reopen_trade_for_chase(self, proposal_id: int, limit_price: float) -> bool:
        """Entry chase: atomically set the new limit AND re-open the ledger row, which the fill
        tracker may have closed ('cancelled') while the chase waited for its own cancel to
        confirm. One UPDATE, so no reader sees a half state. Only a row that never filled is
        touched. Returns False when no row could be re-opened (the caller must not send)."""
        ns_where, ns_args = self._ns_where()
        cur = self.conn.execute(
            f"UPDATE trade_ledger SET status='open', exit_reason=NULL, limit_price=?, updated_at=? "
            f"WHERE proposal_id=? AND entry_fill_price IS NULL AND exit_fill_price IS NULL "
            f"AND status IN ('open','cancelled') AND {ns_where}",
            (float(limit_price), _now(), proposal_id, *ns_args))
        return (cur.rowcount or 0) > 0
