import logging

import pytest

from app.config import env


def test_bool(monkeypatch, caplog):
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("X_B", v)
        assert env._bool("X_B", False) is True
    for v in ("0", "false", "no", "off"):
        monkeypatch.setenv("X_B", v)
        assert env._bool("X_B", True) is False
    monkeypatch.setenv("X_B", "")
    assert env._bool("X_B", True) is True
    monkeypatch.setenv("X_B", "maybe")
    with caplog.at_level(logging.WARNING):
        assert env._bool("X_B", True) is True
    assert "X_B" in caplog.text and "maybe" in caplog.text


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "abc"])
def test_float_bad(monkeypatch, caplog, bad):
    monkeypatch.setenv("X_F", bad)
    with caplog.at_level(logging.WARNING):
        assert env._float("X_F", 1.5) == 1.5
    assert "X_F" in caplog.text


def test_float_ok(monkeypatch):
    monkeypatch.setenv("X_F", "2.5")
    assert env._float("X_F", 1.0) == 2.5


@pytest.mark.parametrize("bad", ["nan", "inf", "1.5", "abc"])
def test_int_bad(monkeypatch, caplog, bad):
    monkeypatch.setenv("X_I", bad)
    with caplog.at_level(logging.WARNING):
        assert env._int("X_I", 7) == 7
    assert "X_I" in caplog.text


def test_int_ok(monkeypatch):
    monkeypatch.setenv("X_I", "42")
    assert env._int("X_I", 7) == 42
