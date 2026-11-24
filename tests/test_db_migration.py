"""An old database file must be upgraded before the first write (no 'no column named')."""
import sqlite3

from app.db import DB, SCHEMA


def _old_db(path):
    """A signals table from before relative_strength / market_* existed."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("DROP TABLE signals")
    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, "
                 "created_at TEXT NOT NULL, symbol TEXT NOT NULL, close REAL)")
    conn.commit()
    conn.close()


def test_first_write_after_upgrade_succeeds(tmp_path):
    path = tmp_path / "t.db"
    _old_db(path)
    db = DB(path)
    sid = db.add_signal(None, {"symbol": "AAPL", "close": 1.0, "relative_strength": 2.5,
                               "market_trend": "up", "market_adx": 20.0})
    assert db.get_signal(sid)["relative_strength"] == 2.5


def test_insert_self_heals_if_column_vanishes(tmp_path):
    path = tmp_path / "t.db"
    db = DB(path)
    # Simulate a stale file swapped in under a running process.
    db.conn.execute("ALTER TABLE signals DROP COLUMN relative_strength")
    sid = db.add_signal(None, {"symbol": "MPWR", "close": 1.0, "relative_strength": 1.5})
    assert db.get_signal(sid)["relative_strength"] == 1.5


def test_unknown_column_still_raises(tmp_path):
    db = DB(tmp_path / "t.db")
    try:
        db.add_signal(None, {"symbol": "X", "not_a_real_column": 1})
    except sqlite3.OperationalError:
        return
    raise AssertionError("expected OperationalError")
