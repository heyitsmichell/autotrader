#!/usr/bin/env python3
"""Backfill MAE/MFE on closed trades + their autopsies (fake 0.0 rows from missing bars).

Recomputes over [entry_fill_time, exit_fill_time] with Portfolio._compute_mae_mfe (same logic as
the app) against a READ-ONLY market-data source (Yahoo via yfinance; the app's Webull session is
never opened and no orders are ever placed). NULL is written when it can't be computed.

    .venv/bin/python scripts/backfill_autopsy_mae.py                 # dry run, prints a table
    .venv/bin/python scripts/backfill_autopsy_mae.py --apply         # backup copy, then write
Options: --db data/trader.db, --only-zero (only rows whose old mae/mfe are 0/NULL).
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class YahooBars:
    """Read-only bar source exposing the two broker methods _compute_mae_mfe uses."""

    def __init__(self) -> None:
        self.source = "yahoo"

    @staticmethod
    def _rows(df) -> list[dict]:
        out = []
        for ts, r in df.dropna(subset=["High", "Low"]).iterrows():
            dt = ts.to_pydatetime()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out.append({"time": dt, "high": float(r["High"]), "low": float(r["Low"])})
        return out

    async def intraday_bars(self, symbol: str, timespan: str = "5m", count: int = 390) -> list[dict]:
        import yfinance as yf
        df = await asyncio.to_thread(yf.download, symbol, period="60d", interval="5m",
                                     auto_adjust=False, progress=False, threads=False)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        return self._rows(df)

    async def daily_bars(self, symbol: str, count: int = 60) -> list[dict]:
        import yfinance as yf
        df = await asyncio.to_thread(yf.download, symbol, period="1y", interval="1d",
                                     auto_adjust=False, progress=False, threads=False)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        out = self._rows(df)
        for b in out:  # daily stamps are exchange-local midnight; pin to the UTC date
            t = b["time"]
            b["time"] = datetime(t.year, t.month, t.day, tzinfo=timezone.utc)
        return out


def _dt(s: str) -> datetime:
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _fmt(v) -> str:
    return "NULL" if v is None else f"{v:.2f}"


async def compute_all(rows: list[dict], source) -> list[dict]:
    from app.portfolio import Portfolio
    fake = SimpleNamespace(broker=source)
    out = []
    for r in rows:
        entry_t, exit_t = _dt(r["entry_fill_time"]), _dt(r["exit_fill_time"])
        if entry_t is None or exit_t is None or exit_t < entry_t:
            # exit recorded before entry = exit matched to the wrong lot; the window is meaningless
            out.append({**r, "new_mae": None, "new_mfe": None, "source": "skip:exit<entry"})
            continue
        try:
            mae, mfe = await Portfolio._compute_mae_mfe(
                fake, r["symbol"], float(r["entry_fill_price"] or 0),
                _dt(r["entry_fill_time"]), _dt(r["exit_fill_time"]))
        except Exception:  # noqa: BLE001
            mae, mfe = None, None
        out.append({**r, "new_mae": mae, "new_mfe": mfe,
                    "source": getattr(source, "source", "bars") if mae is not None else "none"})
    return out


def load_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    q = ("SELECT t.id, t.symbol, t.entry_fill_price, t.entry_fill_time, t.exit_fill_time, "
         "t.mae AS old_mae, t.mfe AS old_mfe FROM trade_ledger t "
         "WHERE t.status='closed' AND t.entry_fill_time IS NOT NULL AND t.exit_fill_time IS NOT NULL "
         "AND EXISTS (SELECT 1 FROM autopsies a WHERE a.trade_id=t.id) ORDER BY t.id")
    return [dict(r) for r in conn.execute(q)]


def print_table(res: list[dict]) -> None:
    print(f"{'id':>4} {'symbol':<7} {'entry':<20} {'exit':<20} {'old mae/mfe':>14} {'new mae/mfe':>14}  source")
    for r in res:
        print(f"{r['id']:>4} {r['symbol']:<7} {str(r['entry_fill_time'])[:19]:<20} "
              f"{str(r['exit_fill_time'])[:19]:<20} "
              f"{_fmt(r['old_mae']) + '/' + _fmt(r['old_mfe']):>14} "
              f"{_fmt(r['new_mae']) + '/' + _fmt(r['new_mfe']):>14}  {r['source']}")


def backup(db: Path) -> Path:
    dest_dir = db.resolve().parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"trader.db.before_mae_backfill_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    src = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def apply(db: Path, res: list[dict]) -> None:
    conn = sqlite3.connect(db, timeout=30)
    try:
        with conn:
            for r in res:
                conn.execute("UPDATE trade_ledger SET mae=?, mfe=? WHERE id=?",
                             (r["new_mae"], r["new_mfe"], r["id"]))
                conn.execute("UPDATE autopsies SET mae_pct=?, mfe_pct=? WHERE trade_id=?",
                             (r["new_mae"], r["new_mfe"], r["id"]))
    finally:
        conn.close()


def run(db: Path, do_apply: bool, source, only_zero: bool = False) -> list[dict]:
    ro = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        rows = load_rows(ro)
    finally:
        ro.close()
    if only_zero:
        rows = [r for r in rows if not r["old_mae"] and not r["old_mfe"]]
    res = asyncio.run(compute_all(rows, source))
    print_table(res)
    if do_apply:
        print("backup:", backup(db))
        apply(db, res)
        print(f"applied {len(res)} rows")
    else:
        print("DRY RUN - nothing written (use --apply)")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "trader.db"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only-zero", action="store_true")
    a = ap.parse_args()
    run(Path(a.db), a.apply, YahooBars(), a.only_zero)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
