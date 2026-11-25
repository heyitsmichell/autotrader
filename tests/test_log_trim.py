import os

from app.broker.transport import _log_tail
from app.logging_setup import trim_log


def _write(p, n):
    p.write_bytes(b"x" * n)


def test_rotates_over_cap(tmp_path):
    p = tmp_path / "a.log"
    _write(p, 200)
    assert trim_log(p, 100) is True
    assert p.stat().st_size == 0
    assert (tmp_path / "a.log.1").stat().st_size == 200


def test_noop_under_cap(tmp_path):
    p = tmp_path / "a.log"
    _write(p, 50)
    assert trim_log(p, 100) is False
    assert p.stat().st_size == 50 and not (tmp_path / "a.log.1").exists()


def test_backup_count_respected(tmp_path):
    p = tmp_path / "a.log"
    for _ in range(10):
        _write(p, 200)
        trim_log(p, 100, backups=3)
    names = sorted(x.name for x in tmp_path.iterdir())
    assert names == ["a.log", "a.log.1", "a.log.2", "a.log.3"]


def test_works_with_other_handle_open(tmp_path):
    p = tmp_path / "a.log"
    with open(p, "ab") as other:
        other.write(b"y" * 200)
        other.flush()
        assert trim_log(p, 100)
        other.write(b"new")
        other.flush()
    assert p.read_bytes() == b"new"
    assert (tmp_path / "a.log.1").stat().st_size == 200


def test_never_raises(tmp_path, monkeypatch):
    assert trim_log(tmp_path / "missing.log", 10) is False
    p = tmp_path / "a.log"
    _write(p, 200)

    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr("app.logging_setup.shutil.copyfile", boom)
    assert trim_log(p, 100) is False


def test_log_tail_offset(tmp_path):
    p = tmp_path / "m.log"
    p.write_text("old line\nnew line\n")
    assert _log_tail(p, 9) == "new line"
    assert _log_tail(p, 10_000) == "new line"  # truncated file: read from 0
    assert _log_tail(tmp_path / "nope", 0) == ""
