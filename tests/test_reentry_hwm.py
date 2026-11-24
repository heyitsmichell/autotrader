"""A re-entry must not inherit the previous trade's high-water mark (live churn 2026-11-24:
TXN/NXPI re-bought, then sold a minute later for 'giving back 100%+ of the peak gain')."""

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Eng:
    def __init__(self, s, trade, kv):
        self.s, self.research = s, None
        self.broker = MagicMock()
        self.broker.positions = AsyncMock(return_value={"TXN": 1.0})
        self.broker.last_price = AsyncMock(return_value=273.0)
        self.signals = MagicMock()
        self.signals.ensure = AsyncMock(return_value={"symbol": "TXN", "close": 273.0, "atr": 6.0,
                                                      "trend": "up", "ta_score": 0.1, "ml_p": None})
        self.db = MagicMock()
        self.db.pending_proposals.return_value = []
        self.db.open_trades.return_value = [trade]
        self.db.signals.return_value = []
        self.db.get_kv.side_effect = lambda k, d=None: kv.get(k, d)
        self.db.set_kv.side_effect = lambda k, v: kv.__setitem__(k, v)
        self.proposed: list[dict] = []

        async def _on_proposal(run_id, p):
            self.proposed.append(p)
            return "ACCEPTED"
        self._on_proposal = _on_proposal

        async def _propose_sell(symbol, qty, run_id, reason, n):
            self.proposed.append({"side": "SELL", "symbol": symbol, "rationale": reason})
            return True
        self._propose_sell = _propose_sell


def _settings(test_settings):
    return dataclasses.replace(test_settings, profit_giveback_pct=20.0,
                               profit_giveback_min_gain_pct=1.0, ml_enabled=False)


def _trade(t):
    return {"symbol": "TXN", "side": "BUY", "entry_fill_price": 274.0, "entry_fill_time": t,
            "created_at": t, "proposal_id": 64}


async def _run(eng):
    from app.engine import Engine
    return await Engine._generate_proposals_deterministic(eng, 1, [])


@pytest.mark.asyncio
async def test_reentry_does_not_inherit_previous_peak(test_settings):
    # the previous TXN trade peaked at 290; the flat-position reset never ran before the re-buy
    kv = {"hwm:TXN": 290.0, "profit_entry:TXN": "2026-11-20T14:30:00+00:00",
          "profit_state_symbols": ["TXN"]}
    eng = _Eng(_settings(test_settings), _trade("2026-11-20T14:40:06+00:00"), kv)
    await _run(eng)
    assert not [p for p in eng.proposed if p.get("side") == "SELL"]
    assert kv["hwm:TXN"] == 274.0
    assert kv["profit_entry:TXN"] == "2026-11-20T14:40:06+00:00"


@pytest.mark.asyncio
async def test_same_entry_keeps_its_peak_and_gives_back(test_settings):
    t = "2026-11-20T14:40:06+00:00"
    kv = {"hwm:TXN": 290.0, "profit_entry:TXN": t, "profit_state_symbols": ["TXN"]}
    eng = _Eng(_settings(test_settings), _trade(t), kv)
    await _run(eng)
    sells = [p for p in eng.proposed if p.get("side") == "SELL"]
    assert sells                    # the kept peak (290) drives a profit-protection exit
