"""Items 17-25: text-file writes, ml dataset lock, db identifier checks, options dte, auth token."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import learning
from app.db import DB
from app.ml_data import store


def test_safe_write_atomic_backup_and_cap(tmp_path):
    p = tmp_path / "strategy.md"
    for i in range(14):
        learning._safe_write(p, f"v{i}")
    assert p.read_text() == "v13\n"
    backups = list((tmp_path / "backups").glob("strategy.md.*"))
    assert len(backups) == learning.BACKUPS_KEPT
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(HTTPException) as e:
        learning._safe_write(p, "x" * (learning.MAX_TEXT_BYTES + 1))
    assert e.value.status_code == 413
    assert p.read_text() == "v13\n"


def test_actor_is_server_side():
    assert learning.ACTOR == "dashboard"


def test_file_lock_exclusive_and_released(tmp_path):
    lock = tmp_path / "d.lock"
    with store._file_lock(lock):
        assert lock.exists()
        with pytest.raises(TimeoutError):
            with store._file_lock(lock, timeout=0.2):
                pass
    assert not lock.exists()


def test_db_rejects_bad_identifiers(tmp_path):
    db = DB(tmp_path / "t.db")
    with pytest.raises(ValueError):
        db._insert("logs; DROP TABLE x", {"a": 1})
    with pytest.raises(ValueError):
        db._insert("logs", {"a b": 1})
    with pytest.raises(ValueError):
        db._update("logs", 1, {"a=1,b": 1})
    db.close()


def test_token_file_written(tmp_path):
    from app.api.auth import write_token_file
    p = tmp_path / "tok"
    write_token_file(p, "abc")
    assert p.read_text() == "abc"
