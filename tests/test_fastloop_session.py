"""FastLoop follows the same session rule as the scheduler (MARKET_HOURS_ONLY / OVERNIGHT_TRADING)."""
from dataclasses import replace

from app import fastloop
from app.config import load_settings
from app.fastloop import FastLoop


def _loop(**kw):
    fl = object.__new__(FastLoop)
    fl.s = replace(load_settings(), **kw)
    return fl


def test_regular_hours_only_by_default_setting(monkeypatch):
    monkeypatch.setattr(fastloop, "market_is_open", lambda: False)
    monkeypatch.setattr(fastloop, "market_is_open_extended", lambda: True)
    assert _loop(market_hours_only=True)._session_open() is False


def test_extended_hours_when_not_market_hours_only(monkeypatch):
    monkeypatch.setattr(fastloop, "market_is_open", lambda: False)
    monkeypatch.setattr(fastloop, "market_is_open_extended", lambda: True)
    assert _loop(market_hours_only=False, overnight_trading=False)._session_open() is True


def test_overnight_session_when_enabled(monkeypatch):
    monkeypatch.setattr(fastloop, "market_is_open", lambda: False)
    monkeypatch.setattr(fastloop, "order_session", lambda: "NIGHT")
    assert _loop(market_hours_only=False, overnight_trading=True)._session_open() is True
