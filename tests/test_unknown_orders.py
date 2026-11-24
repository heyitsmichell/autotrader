"""Tests for unknown order reconciliation."""

import pytest
import uuid
from datetime import timedelta


class TestUnknownOrders:
    """Test unknown order settlement from broker."""

    @pytest.mark.asyncio
    async def test_unknown_orders_settled_from_broker(self, test_engine, mock_broker, test_db):
        """Unknown orders should be settled from the broker."""
        from app.portfolio import parse_orders
        from app import market

        # Place an order in the mock broker to create a filled order in history
        # The mock broker fills LIMIT orders at the current price
        price = await mock_broker.last_price("AAPL")
        coid = uuid.uuid4().hex
        await mock_broker.place_order("AAPL", "BUY", 1, price, coid)

        hist = parse_orders(await mock_broker.order_history_text())
        filled = next((o for o in hist if o.get("status") == "FILLED"), None)
        assert filled is not None, "No filled orders in mock broker history"

        old = (market.now_utc() - timedelta(minutes=30)).isoformat(timespec="seconds")
        row = {"symbol": filled["symbol"], "side": filled["side"], "quantity": 1, "limit_price": 1.0}

        found = test_db.add_proposal(None, row, "unknown", client_order_id=filled["client_order_id"])
        lost = test_db.add_proposal(None, row, "unknown", client_order_id=uuid.uuid4().hex, placed_at=old)
        fresh = test_db.add_proposal(None, row, "unknown", client_order_id=uuid.uuid4().hex)
        unsent = test_db.add_proposal(None, row, "submitting")
        test_db.update_proposal(unsent, created_at=old)

        n = await test_engine.reconcile_orders()
        st = {k: test_db.get_proposal(v)["status"] for k, v in
              dict(found=found, lost=lost, fresh=fresh, unsent=unsent).items()}

        assert n == 3
        assert st["found"] == "placed"
        assert st["lost"] == "failed"
        assert st["unsent"] == "failed"
        assert st["fresh"] == "unknown"

        test_db.update_proposal(fresh, status="failed")