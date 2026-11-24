"""Daily ATM implied volatility per underlying (input for a true IV rank, H3)."""

from __future__ import annotations

from .base import BaseRepo


class AtmIvRepo(BaseRepo):
    """Mixin for the DB facade: atm_iv_history."""

    def record_atm_iv(self, underlying: str, day: str, iv: float) -> None:
        """Upsert one day's ATM IV for an underlying."""
        self.conn.execute(
            "INSERT INTO atm_iv_history (underlying, day, iv) VALUES (?, ?, ?) "
            "ON CONFLICT(underlying, day) DO UPDATE SET iv=excluded.iv",
            (underlying.upper(), day, float(iv)))

    def atm_iv_series(self, underlying: str, days: int = 252) -> list[float]:
        """The last ``days`` daily ATM IVs, oldest first."""
        rows = self._rows(
            "SELECT iv FROM (SELECT day, iv FROM atm_iv_history WHERE underlying=? "
            "AND iv IS NOT NULL ORDER BY day DESC LIMIT ?) ORDER BY day ASC",
            underlying.upper(), int(days))
        return [r["iv"] for r in rows]
