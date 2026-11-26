import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("backfill_mae", ROOT / "scripts" / "backfill_autopsy_mae.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)


class FakeBars:
    source = "fake"

    def __init__(self, fail=()):
        self.fail = set(fail)

    async def intraday_bars(self, symbol, timespan="5m", count=390):
        return []

    async def daily_bars(self, symbol, count=60):
        if symbol in self.fail:
            raise RuntimeError("no data")
        return [{"time": datetime(2026, 11, 24, tzinfo=timezone.utc), "high": 110.0, "low": 95.0}]


def make_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE trade_ledger (id INTEGER PRIMARY KEY, symbol TEXT, entry_fill_price REAL,
      entry_fill_time TEXT, exit_fill_time TEXT, mae REAL, mfe REAL, status TEXT);
    CREATE TABLE autopsies (id INTEGER PRIMARY KEY, trade_id INTEGER, mae_pct REAL, mfe_pct REAL);
    """)
    for i, sym in ((1, "AAA"), (2, "BAD")):
        c.execute("INSERT INTO trade_ledger VALUES (?,?,?,?,?,0.0,0.0,'closed')",
                  (i, sym, 100.0, "2026-11-24T15:00:00Z", "2026-11-24T18:00:00Z"))
        c.execute("INSERT INTO autopsies VALUES (?,?,0.0,0.0)", (i, i))
    c.commit()
    c.close()


def rows(path):
    c = sqlite3.connect(path)
    r = (c.execute("SELECT id,mae,mfe FROM trade_ledger ORDER BY id").fetchall(),
         c.execute("SELECT trade_id,mae_pct,mfe_pct FROM autopsies ORDER BY id").fetchall())
    c.close()
    return r


def test_dry_run_then_apply(tmp_path, capsys):
    db = tmp_path / "trader.db"
    make_db(db)
    before = rows(db)
    res = bf.run(db, False, FakeBars(fail={"BAD"}))
    assert rows(db) == before
    assert not (tmp_path / "backups").exists()
    assert (res[0]["new_mae"], res[0]["new_mfe"]) == (-5.0, 10.0)
    assert (res[1]["new_mae"], res[1]["new_mfe"]) == (None, None)

    bf.run(db, True, FakeBars(fail={"BAD"}))
    led, aut = rows(db)
    assert led == [(1, -5.0, 10.0), (2, None, None)]
    assert aut == [(1, -5.0, 10.0), (2, None, None)]
    backups = list((tmp_path / "backups").glob("trader.db.before_mae_backfill_*"))
    assert len(backups) == 1
    assert rows(backups[0]) == before
