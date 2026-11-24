"""Runnable shim: ``python -m app.mock_mcp_server`` starts the mock MCP server (see app/mock/).

Re-exports the old module's public and private names for existing importers
(price_of, get_stock_bars, _new_order, try_fill, ...).
"""

from .mock.fills import *  # noqa: F401,F403
from .mock.fills import (_detail, _fill, _new_order, _validate_option, _validate_stock)  # noqa: F401
from .mock.options import norm_cdf, option_price, option_symbol  # noqa: F401
from .mock.quotes import BASE_PRICES, DAY, price_of  # noqa: F401
from .mock.server import *  # noqa: F401,F403
from .mock.server import STEPS, mcp  # noqa: F401
from .mock.state import *  # noqa: F401,F403
from .mock.state import _iso, _now_iso, _set, _state_file, _children  # noqa: F401

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
