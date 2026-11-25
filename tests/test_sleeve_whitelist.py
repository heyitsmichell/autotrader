"""The Webull MCP whitelist must include every symbol a sleeve may trade (2026-11-25 incident)."""
import dataclasses

from app.broker.transport import sleeve_symbols
from app.config import load_settings


def test_sleeve_symbols_cover_momentum_vaa_putwrite():
    s = dataclasses.replace(load_settings(), sleeves_enabled=True)
    syms = set(sleeve_symbols(s))
    for x in ("DELL", "HPE", "LITE", "MRNA", "OKTA", "SNDK", "STX", "IEF", "SHY", "LQD", "QQQ", "SPY"):
        assert x in syms, x
    assert len(syms) > 500


def test_sleeve_symbols_empty_when_off():
    assert sleeve_symbols(dataclasses.replace(load_settings(), sleeves_enabled=False)) == []
