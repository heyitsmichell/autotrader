"""Webull broker package (was ``app/broker.py``).

Layout: parsers (pure text parsers), transport (MCP lifecycle/call/rate limit), market_data,
orders, options, crypto (mixins), guard (OrderGuard), sdk (SdkTransport), router (OrderRouter),
core (Broker). Everything the old module exposed is re-exported here.
"""

import random  # noqa: F401  (tests patch ``broker.random.uniform``)

from .core import Broker, mode_warning_for  # noqa: F401
from .errors import BrokerError, OrderGuardError  # noqa: F401
from .guard import FlattenMode, OrderGuard, in_flatten_mode  # noqa: F401
from .orders import _SESSION_ALIASES, resolve_session  # noqa: F401
from .parsers import (ERROR_PREFIXES, STOCK_ACCOUNT_CLASSES, _num, bar_day,  # noqa: F401
                      is_rate_limited, parse_accounts, parse_bar_time, parse_bars,
                      parse_day_pnl, parse_option_positions, parse_option_snapshot,
                      parse_positions, parse_price, parse_snapshot, retry_after_seconds,
                      sanitize, strip_disclaimer)
from .router import OrderRouter, select_execution  # noqa: F401
from .sdk import SdkTransport  # noqa: F401
from .transport import McpTransport, _flatten, _log_tail, is_mutating_tool  # noqa: F401
