"""0006: proposals.order_type (market-orders feature).

limit | marketable | market | chase. NULL on older rows means limit.
"""

from __future__ import annotations

from ..schema import add_column, table_columns


def upgrade(conn) -> None:
    if "order_type" not in table_columns(conn, "proposals"):
        add_column(conn, "proposals", "order_type", "TEXT")
