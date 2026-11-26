"""One-off: correct the ADI trade of proposal 54 (exit logged at 328.00, the earlier trade's
fill; Webull filled its stop at 308.24). Works on a COPY of the DB by default; --apply edits
the given DB in place.

    python scripts/archive/fix_ledger_exit_2026-11-24.py data/trader.db            # copy, prints before/after
    python scripts/archive/fix_ledger_exit_2026-11-24.py data/trader.db --apply    # in place
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROPOSAL_ID, SYMBOL, EXIT_PRICE, REASON = 54, "ADI", 308.24, "stop"
COLS = ("exit_fill_price", "exit_reason", "realized_pnl", "realized_pnl_pct", "exit_slippage",
        "hold_seconds", "exit_fill_time", "entry_fill_price", "entry_fill_time", "status")


def _row(conn):
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM trade_ledger WHERE proposal_id=? AND symbol=?",
                     (PROPOSAL_ID, SYMBOL)).fetchone()
    return None if r is None else {k: r[k] for k in COLS}


def fix(path: str) -> tuple[dict, dict]:
    conn = sqlite3.connect(path)
    try:
        before = _row(conn)
        if before is None:
            sys.exit(f"no {SYMBOL} trade for proposal {PROPOSAL_ID} in {path}")
        t = conn.execute("SELECT entry_fill_price, quantity_filled, entry_commission, exit_commission, "
                         "stop_price FROM trade_ledger WHERE proposal_id=?", (PROPOSAL_ID,)).fetchone()
        entry, qty, ec, xc, stop = t[0], t[1] or 1, t[2] or 0.0, t[3] or 0.0, t[4]
        pnl = (EXIT_PRICE - entry) * qty - ec - xc
        pct = (EXIT_PRICE / entry - 1) * 100
        # exit time: the trade's own stop row was marked filled when it fired (~11:01 ET)
        r = conn.execute("SELECT updated_at FROM protective_orders WHERE proposal_id=? AND kind='sl' "
                         "AND status='filled' ORDER BY id DESC", (PROPOSAL_ID,)).fetchone()
        xt = r[0] if r else before["exit_fill_time"]
        hold = int((datetime.fromisoformat(xt.replace("Z", "+00:00"))
                    - datetime.fromisoformat(before["entry_fill_time"].replace("Z", "+00:00"))).total_seconds())
        conn.execute("UPDATE trade_ledger SET exit_fill_price=?, exit_reason=?, realized_pnl=?, "
                     "realized_pnl_pct=?, exit_slippage=?, exit_fill_time=?, hold_seconds=?, "
                     "updated_at=datetime('now') WHERE proposal_id=?",
                     (EXIT_PRICE, REASON, pnl, pct, (stop - EXIT_PRICE) if stop else None, xt, hold,
                      PROPOSAL_ID))
        conn.commit()
        return before, _row(conn)
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--apply", action="store_true", help="edit the DB in place (default: a copy)")
    ap.add_argument("--out", help="copy destination (default: <db>.fixed-copy)")
    a = ap.parse_args()
    target = a.db
    if not a.apply:
        target = a.out or a.db + ".fixed-copy"
        src = sqlite3.connect(f"file:{Path(a.db).resolve().as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        src.backup(dst)
        src.close()
        dst.close()
    before, after = fix(target)
    print(f"target: {target}")
    for k in COLS:
        print(f"  {k}: {before[k]} -> {after[k]}")


if __name__ == "__main__":
    main()
