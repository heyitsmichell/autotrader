"""DB package: connection PRAGMAs, close(), migrations, duplicate guard, prune, atm_iv,
run params and lesson skips."""
import sqlite3
import threading
from datetime import timedelta

import pytest

from app.db import DB, SCHEMA, DuplicateProposalError, current_version, latest_version
from app.market import now_utc


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "t.db", "mock")
    yield d
    d.close()


def _prop(sym="AAPL", side="BUY"):
    return {"symbol": sym, "side": side, "quantity": 1, "limit_price": 10.0}


def test_pragmas(db):
    q = lambda p: db.conn.execute(f"PRAGMA {p}").fetchone()[0]
    assert q("journal_mode") == "wal"
    assert q("busy_timeout") == 5000
    assert q("synchronous") == 1          # NORMAL
    assert q("foreign_keys") == 1


def test_close_is_idempotent_and_releases_file(tmp_path):
    path = tmp_path / "t.db"
    d = DB(path)
    d.log("info", "hello")
    d.close()
    d.close()                              # second call is a no-op
    with pytest.raises(sqlite3.ProgrammingError):
        d.conn.execute("SELECT 1")
    path.unlink()                          # would raise WinError 32 if still open
    assert not path.exists()


def test_close_checkpoints_wal(tmp_path):
    path = tmp_path / "t.db"
    d = DB(path)
    d.log("info", "x")
    d.close()
    wal = tmp_path / "t.db-wal"
    assert (not wal.exists()) or wal.stat().st_size == 0


def test_user_version_and_idempotent_migrations(tmp_path):
    path = tmp_path / "t.db"
    d = DB(path)
    assert current_version(d.conn) == latest_version() >= 4
    d.close()
    d2 = DB(path)                          # re-running must change nothing and not raise
    assert current_version(d2.conn) == latest_version()
    d2.close()


def test_migration_0001_on_legacy_unversioned_db(tmp_path):
    """A pre-migration database (full schema, user_version 0) upgrades in place."""
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.executescript(SCHEMA)
    raw.execute("INSERT INTO events (ts, level, message) VALUES ('2020-01-01T00:00:00+00:00','info','old')")
    raw.commit()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    raw.close()
    d = DB(path, "mock")
    assert current_version(d.conn) == latest_version()
    cols = {r[1] for r in d.conn.execute("PRAGMA table_info(lessons)")}
    assert {"apply_state", "apply_reason", "updated_at"} <= cols
    d.close()


def test_newer_database_version_is_left_alone(tmp_path):
    path = tmp_path / "t.db"
    DB(path).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version=999")
    raw.close()
    d = DB(path)
    assert current_version(d.conn) == 999
    d.close()


def test_duplicate_open_proposal_rejected(db):
    db.add_proposal(None, _prop(), "pending")
    with pytest.raises(DuplicateProposalError):
        db.add_proposal(None, _prop(), "pending")
    assert issubclass(DuplicateProposalError, ValueError)
    db.add_proposal(None, _prop(side="SELL"), "pending")     # other side is fine
    db.add_proposal(None, _prop(sym="MSFT"), "pending")      # other symbol is fine


def test_duplicate_allowed_once_first_is_closed(db):
    pid = db.add_proposal(None, _prop(), "pending")
    db.update_proposal(pid, status="dismissed")
    db.add_proposal(None, _prop(), "pending")
    db.add_proposal(None, _prop(), "rejected")               # closed statuses never conflict
    db.add_proposal(None, _prop(), "placed")


def test_submitting_counts_as_open(db):
    a = db.add_proposal(None, _prop(), "pending")
    assert db.claim_pending(a)                               # pending -> submitting
    with pytest.raises(DuplicateProposalError):
        db.add_proposal(None, _prop(), "pending")


def test_migration_dismisses_existing_duplicates(tmp_path):
    path = tmp_path / "t.db"
    raw = sqlite3.connect(path)
    raw.executescript(SCHEMA)
    raw.execute("ALTER TABLE proposals ADD COLUMN broker_mode TEXT")   # pre-0001 namespace column
    ins = ("INSERT INTO proposals (created_at, symbol, side, quantity, limit_price, status, "
           "broker_mode) VALUES ('2026-01-01T00:00:00+00:00', ?, 'BUY', 1, 1.0, ?, 'mock')")
    for sym, status in [("AAPL", "pending"), ("AAPL", "pending"), ("AAPL", "pending"),
                        ("MSFT", "submitting"), ("MSFT", "pending"), ("NVDA", "pending")]:
        raw.execute(ins, (sym, status))
    raw.commit()
    raw.close()
    d = DB(path, "mock")
    rows = {r["id"]: r for r in d._rows("SELECT * FROM proposals")}
    assert [rows[i]["status"] for i in (1, 2, 3)] == ["dismissed", "dismissed", "pending"]
    assert rows[1]["reason"] == "duplicate (migration)"
    assert (rows[4]["status"], rows[5]["status"]) == ("submitting", "dismissed")
    assert rows[6]["status"] == "pending"
    d.close()


def test_prune_only_touches_retention_tables(db):
    old = (now_utc() - timedelta(days=200)).isoformat(timespec="seconds")
    new = now_utc().isoformat(timespec="seconds")
    c = db.conn
    for ts in (old, new):
        c.execute("INSERT INTO events (ts, level, message) VALUES (?, 'info', 'm')", (ts,))
        c.execute("INSERT INTO llm_usage (ts, purpose) VALUES (?, 'analysis')", (ts,))
        c.execute("INSERT INTO equity_snapshots (ts, net_liq) VALUES (?, 1)", (ts,))
        c.execute("INSERT INTO news_items (news_id, symbol, fetched_at) VALUES (?, 'AAPL', ?)", (ts, ts))
    pid = db.add_proposal(None, _prop(), "dismissed")
    c.execute("UPDATE proposals SET created_at=? WHERE id=?", (old, pid))
    out = db.prune(90)
    assert out == {"news_items": 1, "events": 1, "llm_usage": 1, "equity_snapshots": 1}
    for t in out:
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 1
    assert db.get_proposal(pid) is not None                  # proposals are never pruned
    assert db.prune(90) == {t: 0 for t in out}               # idempotent
    assert db.prune() == {t: 0 for t in out}                 # default retention


def test_retention_indexes_exist(db):
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master")}
    assert {"ux_proposals_open", "ix_events_ts", "ix_llm_usage_ts", "ix_equity_snapshots_ts",
            "ix_news_items_symbol_fetched", "ix_news_items_fetched"} <= names


def test_atm_iv_series(db):
    assert db.atm_iv_series("SPY") == []
    for day, iv in [("2026-01-03", 0.3), ("2026-01-01", 0.1), ("2026-01-02", 0.2)]:
        db.record_atm_iv("SPY", day, iv)
    db.record_atm_iv("SPY", "2026-01-02", 0.25)              # upsert
    db.record_atm_iv("QQQ", "2026-01-02", 0.9)
    assert db.atm_iv_series("SPY") == [0.1, 0.25, 0.3]       # oldest first
    assert db.atm_iv_series("SPY", days=2) == [0.25, 0.3]    # the most recent N
    assert db.atm_iv_series("QQQ") == [0.9]


def test_run_params_round_trip(db):
    rid = db.create_run("manual")
    assert db.get_run_params(rid) is None
    db.save_run_params(rid, {"ml_min_p": 0.52, "watchlist": ["AAPL"]})
    assert db.get_run_params(rid) == {"ml_min_p": 0.52, "watchlist": ["AAPL"]}
    db.save_run_params(rid, {"ml_min_p": 0.6})
    assert db.get_run_params(rid) == {"ml_min_p": 0.6}


def test_lesson_skip_and_applied(db):
    lid = db.add_lesson(param_name="sl_atr_mult", param_value=2.0, param_scope="global",
                        sample_size=10, created_by="autopsy")
    row = db.get_lesson(lid)
    assert (row["apply_state"], row["apply_reason"]) == ("pending", None)
    db.record_lesson_skip(lid, "sample 10 < 30")
    row = db.lessons()[0]
    assert (row["apply_state"], row["apply_reason"]) == ("skipped", "sample 10 < 30")
    db.record_lesson_applied(lid, "p=0.01")
    assert db.get_lesson(lid)["apply_state"] == "applied"
    db.set_lesson_status(lid, "active")                      # updated_at column now exists
    assert db.get_lesson(lid)["status"] == "active"


def test_concurrent_use_of_shared_connection(db):
    errors = []

    def work(n):
        try:
            for i in range(50):
                db.log("info", f"t{n}-{i}")
                db.events(5)
        except Exception as e:   # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 300
