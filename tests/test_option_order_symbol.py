"""Option orders must send the UNDERLYING as `symbol` (Webull whitelist / API), never the OCC code."""
import pytest

from app.broker.options import order_symbol


def test_order_symbol_maps_occ_to_underlying():
    assert order_symbol("QQQ261218P00584780") == "QQQ"
    assert order_symbol("aapl240119c00150000") == "AAPL"
    assert order_symbol("QQQ") == "QQQ"           # already a ticker: unchanged


@pytest.mark.asyncio
async def test_place_option_single_order_sends_underlying(monkeypatch):
    from app.broker.options import OptionsMixin

    class B(OptionsMixin):
        def _guard(self, *a, **k):
            pass

        async def account_id(self):
            return "A1"

        async def _submit(self, tool, **args):
            self.sent = (tool, args)
            return "ok"

    b = B()
    await b.place_option_single_order("QQQ261218P00584000", "SELL", 2, "PUT", 584.0, "2026-12-18", 15.0,
                                      "sp" + "0" * 30, position_intent="SELL_TO_OPEN")
    tool, args = b.sent
    assert tool == "place_option_single_order"
    assert args["symbol"] == "QQQ" and args["strike_price"] == 584.0 and args["option_type"] == "PUT"
