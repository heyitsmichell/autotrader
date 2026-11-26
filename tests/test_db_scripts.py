"""scripts/backup_db.py and scripts/migrate_db_namespace.py."""
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from app.db import DB

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name.split("/")[-1], SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backup_verifies_and_writes_to_dest(tmp_path):
    backup_db = _load("backup_db")
    src = tmp_path / "trader.db"
    d = DB(src)
    d.log("info", "x")
    d.close()
    dest = tmp_path / "offdisk"
    out = backup_db.backup_database(src, dest, keep=2)
    assert out.parent == dest and out.exists()
    assert backup_db.verify_backup(out) == "ok"
    assert backup_db.main.__module__            # CLI entry exists
    for _ in range(3):
        backup_db.backup_database(src, dest, keep=2)
    assert len(list(dest.glob("trader.backup-*"))) <= 3   # pruned (same-second names collapse)


def test_backup_fails_on_corrupt_copy(tmp_path, monkeypatch):
    backup_db = _load("backup_db")
    src = tmp_path / "trader.db"
    DB(src).close()
    monkeypatch.setattr(backup_db, "verify_backup", lambda p: "*** in database main ***")
    with pytest.raises(backup_db.BackupIntegrityError):
        backup_db.backup_database(src, tmp_path / "b")
    assert not list((tmp_path / "b").glob("*.db"))          # the bad copy is removed
    monkeypatch.setattr(sys, "argv", ["backup_db", str(src), "--dest", str(tmp_path / "b2")])
    assert backup_db.main() == 1


def test_migrate_refuses_while_app_lock_is_held(tmp_path, monkeypatch):
    mig = _load("archive/migrate_db_namespace")
    lock = tmp_path / "trader.lock"
    lock.write_text(f"{os.getpid()}:1")                     # this process is alive
    assert mig.app_is_running(lock) is True
    lock.write_text("99999999:0")                           # dead pid, no port
    assert mig.app_is_running(lock) is False
    assert mig.app_is_running(tmp_path / "missing.lock") is False

    path = tmp_path / "trader.db"
    d = DB(path, "mock")
    d.log("info", "App started. Broker: Mock broker (simulated)")
    d.close()
    monkeypatch.setattr(mig, "LOCK_FILE", lock)
    lock.write_text(f"{os.getpid()}:1")
    assert mig.main(["--db", str(path)]) == 2               # refused
    lock.unlink()
    assert mig.main(["--db", str(path)]) == 0               # runs through the migration runner
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 4
    conn.close()
