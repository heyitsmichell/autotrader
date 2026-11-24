"""Protective (stop / take-profit) orders"""

from __future__ import annotations

from typing import Any

from .schema import _now
from .base import BaseRepo


class ProtectiveRepo(BaseRepo):
    """Mixin for the DB facade: protective (stop / take-profit) orders."""

    # -- protective orders ---------------------------------------------------------
    def add_protective(self, proposal_id: int | None, symbol: str, kind: str, coid: str,
                       qty: float, price: float, status: str, note: str = "") -> int:
        return self._insert("protective_orders", {
            "proposal_id": proposal_id, "symbol": symbol, "kind": kind, "client_order_id": coid,
            "quantity": qty, "price": price, "status": status, "created_at": _now(),
            "updated_at": _now(), "note": note,
            "broker_mode": self.broker_mode, "webull_environment": self.webull_environment, "account_id": self.account_id})

    def protective(self, symbol: str | None = None, open_only: bool = True) -> list[dict]:
        ns_where, ns_args = self._ns_where()
        where = ["status IN ('waiting','active','unknown')", ns_where] if open_only else [ns_where]
        args: list = [*ns_args]
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        sql = "SELECT * FROM protective_orders"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self._rows(sql + " ORDER BY id DESC LIMIT 200", *args)

    def set_protective(self, pid: int, status: str, note: str | None = None,
                       **fields: Any) -> None:
        ns_where, ns_args = self._ns_where()
        fields.update(status=status, updated_at=_now())
        if note is not None:
            fields["note"] = note
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE protective_orders SET {cols} WHERE id=? AND {ns_where}", (*fields.values(), pid, *ns_args))

    def restorable_stops(self) -> list[dict]:
        """Stops cancelled to make room for a sell whose outcome isn't settled yet."""
        ns_where, ns_args = self._ns_where()
        return self._rows(f"SELECT * FROM protective_orders WHERE kind='sl' AND status='cancelled' AND sell_coid IS NOT NULL AND {ns_where} ORDER BY id DESC", *ns_args)

    def settle_stops(self, sell_coid: str, note: str) -> None:
        """The sell is resolved: these cancelled stops no longer need restoring."""
        ns_where, ns_args = self._ns_where()
        self.conn.execute(f"UPDATE protective_orders SET sell_coid=NULL, note=?, updated_at=? WHERE sell_coid=? AND {ns_where}", (note, _now(), sell_coid, *ns_args))

    def retag_stops(self, old_sell_coid: str, new_sell_coid: str) -> None:
        """The sell a cancelled stop made room for was re-sent under a new client order id."""
        ns_where, ns_args = self._ns_where()
        self.conn.execute(f"UPDATE protective_orders SET sell_coid=?, updated_at=? "
                          f"WHERE sell_coid=? AND {ns_where}",
                          (new_sell_coid, _now(), old_sell_coid, *ns_args))
