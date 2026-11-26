import sqlite3
import types

import pytest

from app.db import DB
from app.db.connection import connect
from app.db.migrations import current_version, latest_version, run_migrations
from app.db import migrations as mig


def test_transaction_commit_rollback_nested(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE t (x INTEGER)")
    with conn.transaction():
        conn.execute("INSERT INTO t VALUES (1)")
        with conn.transaction():
            conn.execute("INSERT INTO t VALUES (2)")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    with pytest.raises(RuntimeError):
        with conn.transaction():
            conn.execute("INSERT INTO t VALUES (3)")
            with conn.transaction():
                conn.execute("INSERT INTO t VALUES (4)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    assert not conn.raw.in_transaction
    conn.close()


def test_migrations_idempotent(tmp_path):
    db = DB(tmp_path / "m.db")
    v = current_version(db.conn)
    assert v == latest_version()
    assert run_migrations(db.conn) == v
    db.conn.close()
    db2 = DB(tmp_path / "m.db")
    assert current_version(db2.conn) == v
    db2.conn.close()


def test_failing_migration_rolls_back(tmp_path, monkeypatch):
    db = DB(tmp_path / "f.db")
    v = current_version(db.conn)

    def bad(conn):
        conn.execute("CREATE TABLE half_done (x)")
        raise sqlite3.OperationalError("fail")

    fake = types.SimpleNamespace(upgrade=bad)
    monkeypatch.setattr(mig, "discover", lambda: [(v + 1, "9999_bad", fake)])
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(db.conn)
    assert current_version(db.conn) == v
    assert db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='half_done'").fetchone() is None
    db.conn.close()


def test_rollback_lesson_validation_and_atomic(tmp_path):
    db = DB(tmp_path / "l.db")
    base = dict(param_name="p", param_value=1.0, param_scope="g", status="proposed", sample_size=40, created_by="t",
                version=1)
    a = db.add_lesson(**base)
    db.set_lesson_status(a, "approved", approved_by="h")
    db.set_lesson_status(a, "active")
    b = db.create_lesson_version(a, 2.0)
    with pytest.raises(ValueError):          # b not active yet
        db.rollback_lesson(b, "h")
    db.set_lesson_status(b, "approved", approved_by="h")
    db.set_lesson_status(b, "active")
    assert db.rollback_lesson(b, "h") == a
    assert db.get_lesson(a)["status"] == "active"
    assert db.get_lesson(b)["status"] == "retired"
    db.conn.close()
