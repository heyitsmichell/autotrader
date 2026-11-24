"""Protective stops outside regular hours (CORE/GTC) and alert dedupe."""
from types import SimpleNamespace

import pytest

from tests.test_broker_guard import FakeSession, _broker, _result
from app.broker import BrokerError
from app.broker.orders import resolve_session
from app.engine.reconcile import ReconcileMixin


@pytest.fixture
def premarket(monkeypatch):
    monkeypatch.setattr("app.broker.orders.market.order_session", lambda at=None: "ALL")


@pytest.mark.asyncio
async def test_stop_in_premarket_uses_core_gtc(premarket):
    sess = FakeSession([_result("client_order_id: x")])
    await _broker(sess).place_stop("MSFT", 1, 450.0, "c1")
    name, args = sess.calls[0]
    assert name == "place_stock_order"
    assert args["trading_session"] == "CORE" and args["time_in_force"] == "GTC"
    assert args["order_type"] == "STOP_LOSS"


def test_entries_still_validated(premarket):
    with pytest.raises(BrokerError):
        resolve_session("CORE")                       # entry: CORE not open now
    assert resolve_session("CORE", queue_for_core=True) == "CORE"
    assert resolve_session("AUTO") == "ALL"


def test_closed_market_still_refused(monkeypatch):
    monkeypatch.setattr("app.broker.orders.market.order_session", lambda at=None: None)
    with pytest.raises(BrokerError):
        resolve_session("CORE", queue_for_core=True)


@pytest.mark.asyncio
async def test_gtx_error_retries_with_core(premarket):
    sess = FakeSession([_result("[OPENAPI_GTX_NOT_SUPPORT_MARKET] no", error=True),
                        _result("client_order_id: x")])
    b = _broker(sess)
    await b.place_stop("MSFT", 1, 450.0, "c1", trading_session="ALL")
    assert [c[1]["trading_session"] for c in sess.calls] == ["ALL", "CORE"]


class _Eng(ReconcileMixin):
    def __init__(self):
        self.logs = []
        self.db = SimpleNamespace(log=lambda lvl, msg, alert=False: self.logs.append((lvl, alert)))


def test_alert_dedupe():
    e = _Eng()
    for _ in range(5):
        e._stop_alert("TLT", "no_stop", "error", "m")
    assert e.logs == [("error", True)]
    e._stop_alert("TLT", "other", "error", "m")
    assert len(e.logs) == 2
    e._stop_alerts_clear({"TLT"}, "TLT")
    e._stop_alert("TLT", "no_stop", "error", "m")
    assert e.logs[-1] == ("error", True) and len(e.logs) == 3
    e._stop_alerts_clear(set())                       # position closed
    assert not e._stop_alerts
