import asyncio
import types
from datetime import datetime

from app import scanner_log
from app.db import DB

TXT = """=== Most Active ===
  AAPL - Apple  (instrument_id: 1)
    Price: 190.5  Pre Close: 1  Open: 1  High: 2  Low: 1  Close: 1
    Change: 1  Change Ratio: 0.0123  Amplitude: 1
    Volume: 1,000  Turnover: 1  Turnover Rate: 1  Market Value: 5
  MSFT - Microsoft  (instrument_id: 2)
    Price: 400  Change Ratio: -0.5
    Volume: 7  Turnover: 1
"""


class FakeBroker:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def call(self, name, **a):
        self.calls.append(name)
        if self.fail:
            raise RuntimeError("no tool")
        return TXT


def _eng(tmp_path, mode="live", fail=False):
    return types.SimpleNamespace(s=types.SimpleNamespace(broker_mode=mode), db=DB(tmp_path / "t.db"),
                                 broker=FakeBroker(fail))


def _at(monkeypatch, y, m, d, h):
    monkeypatch.setattr(scanner_log, "now_et", lambda: datetime(y, m, d, h, 45))


def test_parse():
    rows = scanner_log.parse_scanner(TXT)
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]
    assert rows[0]["price"] == 190.5 and rows[0]["volume"] == 1000 and rows[0]["change_pct"] == 0.0123


def test_logs_once_per_day(tmp_path, monkeypatch):
    e = _eng(tmp_path)
    _at(monkeypatch, 2026, 11, 25, 17)         # Wednesday
    out = asyncio.run(scanner_log.log_scanners(e))
    assert out["most_active"] == 2
    assert e.db.scanner_snapshot("2026-11-25", "gainers")[0]["symbol"] == "AAPL"
    n = len(e.broker.calls)
    assert asyncio.run(scanner_log.log_scanners(e)) == {} and len(e.broker.calls) == n


def test_skips(tmp_path, monkeypatch):
    e = _eng(tmp_path)
    _at(monkeypatch, 2026, 11, 25, 15)         # before 16:30
    assert asyncio.run(scanner_log.log_scanners(e)) == {}
    _at(monkeypatch, 2026, 11, 28, 17)         # Saturday
    assert asyncio.run(scanner_log.log_scanners(e)) == {}
    _at(monkeypatch, 2026, 11, 26, 17)         # Thanksgiving
    assert asyncio.run(scanner_log.log_scanners(e)) == {}
    assert e.broker.calls == []
    m = _eng(tmp_path / "x" if False else tmp_path, mode="mock")
    _at(monkeypatch, 2026, 11, 25, 17)
    assert asyncio.run(scanner_log.log_scanners(m)) == {} and m.broker.calls == []


def test_failure_never_raises(tmp_path, monkeypatch):
    e = _eng(tmp_path, fail=True)
    _at(monkeypatch, 2026, 11, 25, 17)
    assert asyncio.run(scanner_log.log_scanners(e)) == {}
