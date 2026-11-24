"""Tests for bracket order flow."""

import pytest
import uuid


class TestBracketOrders:
    """Test bracket order placement and management."""

    @pytest.mark.asyncio
    async def test_accepts_valid_buy_with_bracket(self, test_engine, bullish_signal, mock_broker):
        """Should accept a valid BUY with a bracket."""
        price = await mock_broker.last_price("AAPL")
        r = await test_engine._on_proposal(
            test_engine.db.create_run("test"),
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        assert r.startswith("ACCEPTED")
        assert "Bracket" in r

    @pytest.mark.asyncio
    async def test_buy_placed_as_bracket(self, test_engine, bullish_signal, mock_broker):
        """BUY should be placed as a bracket."""
        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        p = await test_engine.execute(pid, actor="user")
        assert p["status"] == "placed"

    @pytest.mark.asyncio
    async def test_position_shows_shares(self, test_engine, bullish_signal, mock_broker):
        """Position should show correct shares."""
        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        await test_engine.execute(pid, actor="user")
        positions = await mock_broker.positions()
        assert positions.get("AAPL", 0) == 4

    @pytest.mark.asyncio
    async def test_stop_and_target_recorded(self, test_engine, bullish_signal, mock_broker):
        """Stop and target should be recorded."""
        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        await test_engine.execute(pid, actor="user")
        prot = test_engine.db.protective("AAPL")
        kinds = sorted(o["kind"] for o in prot)
        assert kinds == ["sl", "tp"]

    @pytest.mark.asyncio
    async def test_exits_armed_after_entry_filled(self, test_engine, bullish_signal, mock_broker):
        """Exits should be armed after entry filled."""
        from app.portfolio import Portfolio

        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        await test_engine.execute(pid, actor="user")

        portfolio = Portfolio(test_engine.s, test_engine.db, mock_broker)
        await portfolio.reconcile()
        prot = test_engine.db.protective("AAPL")
        assert all(o["status"] == "active" for o in prot)

    @pytest.mark.asyncio
    async def test_open_orders_parser_sees_both_exit_legs(self, test_engine, bullish_signal, mock_broker):
        """Open orders parser should see both exit legs."""
        from app.portfolio import parse_orders

        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        await test_engine.execute(pid, actor="user")

        opened = parse_orders(await mock_broker.open_orders_text())
        prot = test_engine.db.protective("AAPL")
        prot_coids = {o["client_order_id"] for o in prot}
        opened_coids = {o["client_order_id"] for o in opened}
        assert prot_coids <= opened_coids

    @pytest.mark.asyncio
    async def test_positions_parsed(self, test_engine, bullish_signal, mock_broker):
        """Positions should be parsed correctly."""
        from app.portfolio import parse_position_rows

        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        await test_engine.execute(pid, actor="user")

        rows = parse_position_rows(await mock_broker.positions_text())
        assert rows and rows[0]["symbol"] == "AAPL"
        assert rows[0]["market_value"] is not None

    @pytest.mark.asyncio
    async def test_partial_exit_reprotects_rest(self, test_engine, bullish_signal, mock_broker):
        """Partial exit should re-protect the rest."""
        from app.portfolio import Portfolio

        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        await test_engine.execute(pid, actor="user")

        # Sell 2 shares
        price = await mock_broker.last_price("AAPL")
        r = await test_engine._on_proposal(
            test_engine.db.create_run("test"),
            dict(symbol="AAPL", side="SELL", quantity=2, limit_price=round(price * 0.995, 2),
                 confidence=0.5, rationale="test")
        )
        assert r.startswith("ACCEPTED")
        pid = int(r.split("#")[1].split(":")[0])
        p = await test_engine.execute(pid, actor="user")
        assert p["status"] == "placed"
        assert (await mock_broker.positions()).get("AAPL", 0) == 2

        prot = test_engine.db.protective("AAPL")
        assert len(prot) == 1
        assert prot[0]["kind"] == "sl"
        assert prot[0]["quantity"] == 2

    @pytest.mark.asyncio
    async def test_bracket_legs_cancelled_at_broker(self, test_engine, bullish_signal, mock_broker):
        """Bracket legs should be cancelled at the broker."""
        from app.portfolio import parse_orders

        price = await mock_broker.last_price("AAPL")
        run_id = test_engine.db.create_run("test")
        r = await test_engine._on_proposal(
            run_id,
            dict(symbol="AAPL", side="BUY", quantity=4, limit_price=round(price * 1.005, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        await test_engine.execute(pid, actor="user")

        # Partial sell
        price = await mock_broker.last_price("AAPL")
        r = await test_engine._on_proposal(
            test_engine.db.create_run("test"),
            dict(symbol="AAPL", side="SELL", quantity=2, limit_price=round(price * 0.995, 2),
                 confidence=0.5, rationale="test")
        )
        pid = int(r.split("#")[1].split(":")[0])
        await test_engine.execute(pid, actor="user")

        hist = {o["client_order_id"]: o["status"] for o in parse_orders(await mock_broker.order_history_text())}
        cancelled = [o for o in test_engine.db.protective("AAPL", open_only=False) if o["status"] == "cancelled"]
        assert cancelled
        assert all(hist.get(o["client_order_id"]) == "CANCELLED" for o in cancelled)