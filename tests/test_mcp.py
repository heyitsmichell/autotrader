"""Tests for MCP connection and tools."""

import pytest


class TestMCPConnection:
    """Test MCP server connection and available tools."""

    @pytest.mark.asyncio
    async def test_connected_to_mock_mcp_server(self, mock_broker):
        """Should connect to mock MCP server."""
        assert mock_broker.connected is True

    @pytest.mark.asyncio
    async def test_server_lists_order_combo_and_quote_tools(self, mock_broker):
        """Server should list order, combo, and quote tools."""
        tool_names = {t.name for t in mock_broker.tools}
        required = {"place_stock_order", "place_stock_combo_order", "get_stock_snapshot"}
        assert required <= tool_names

    @pytest.mark.asyncio
    async def test_account_id_parsed(self, mock_broker):
        """Account ID should be parsed correctly."""
        acct = await mock_broker.account_id()
        assert acct == "MOCK0001"

    @pytest.mark.asyncio
    async def test_aapl_price_parsed(self, mock_broker):
        """AAPL price should be parsed correctly."""
        price = await mock_broker.last_price("AAPL")
        assert price is not None and price > 0


class TestBrokerToolCoverage:
    """The LLM analyst (app/agent.py) was deleted, so its tool-allowlist tests went with it.
    What still matters is that the broker exposes the read tools the engine depends on."""

    @pytest.mark.asyncio
    async def test_server_lists_read_tools_the_engine_uses(self, mock_broker):
        tool_names = {t.name for t in mock_broker.tools}
        required = {"get_stock_bars", "get_account_positions", "get_account_balance",
                    "get_stock_news"}
        assert required <= tool_names

    @pytest.mark.asyncio
    async def test_positions_and_balance_parse(self, mock_broker):
        assert isinstance(await mock_broker.positions(), dict)
        assert (await mock_broker.account_balance()).get("net_liq", 0) > 0
