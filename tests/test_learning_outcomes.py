"""Learner.evaluate_outcomes: option (OCC) proposals are not scored with stock bars."""
import pytest

from app.learning import Learner


class _FakeDB:
    def __init__(self, proposals, signals):
        self._p, self._s = proposals, signals
        self.updated = []

    def proposals_needing_outcomes(self, older_than, newer_than):
        return list(self._p)

    def signals_needing_outcomes(self, older_than, newer_than):
        return list(self._s)

    def update_proposal(self, pid, **fields):
        self.updated.append(pid)

    def update_signal(self, sid, **fields):
        pass

    def log(self, *a, **k):
        pass


class _FakeBroker:
    def __init__(self):
        self.symbols = []

    async def call(self, tool, **kw):
        self.symbols.append(kw.get("symbols"))
        return ""          # no bars -> nothing scored, but the call is recorded


def _row(pid, sym):
    return {"id": pid, "symbol": sym, "created_at": "2026-11-10T15:00:00+00:00", "side": "BUY",
            "ref_price": 1.11, "limit_price": 1.11, "ret_1d": None, "ret_5d": None}


@pytest.mark.asyncio
async def test_option_proposals_skip_stock_bars(test_settings):
    db = _FakeDB([_row(1, "TLT261231C00082000"), _row(2, "AAPL")],
                 [{**_row(3, "QQQ261218P00580000"), "close": 7.9}])
    br = _FakeBroker()
    await Learner(test_settings, br, db).evaluate_outcomes()
    assert "TLT261231C00082000" not in br.symbols
    assert "QQQ261218P00580000" not in br.symbols
    assert "AAPL" in br.symbols and "SPY" in br.symbols


@pytest.mark.asyncio
async def test_only_option_rows_means_no_broker_calls(test_settings):
    db = _FakeDB([_row(1, "TLT261231C00082000")], [])
    br = _FakeBroker()
    assert await Learner(test_settings, br, db).evaluate_outcomes() == 0
    assert br.symbols == []
