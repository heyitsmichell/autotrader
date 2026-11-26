"""Daily scanner snapshots (most active / gainers / losers / 52-week highs)."""

from __future__ import annotations

import json

from .base import BaseRepo


class ScannerRepo(BaseRepo):
    """Mixin for the DB facade: scanner_snapshots."""

    def save_scanner_snapshot(self, day: str, scanner: str, rows: list[dict]) -> int:
        """Replace one (day, scanner) snapshot. Each row: {symbol, ...fields}; rank = position."""
        with self.conn.transaction():
            self.conn.execute("DELETE FROM scanner_snapshots WHERE day=? AND scanner=?", (day, scanner))
            for i, r in enumerate(rows, 1):
                self.conn.execute(
                    "INSERT OR REPLACE INTO scanner_snapshots (day, scanner, rank, symbol, fields_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (day, scanner, i, str(r["symbol"]).upper(), json.dumps(r, default=str)))
        return len(rows)

    def scanner_snapshot(self, day: str, scanner: str) -> list[dict]:
        rows = self._rows("SELECT rank, symbol, fields_json FROM scanner_snapshots "
                          "WHERE day=? AND scanner=? ORDER BY rank", day, scanner)
        return [{"rank": r["rank"], "symbol": r["symbol"], **json.loads(r["fields_json"] or "{}")}
                for r in rows]
