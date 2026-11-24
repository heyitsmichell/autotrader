"""0005: news_items.scorer / event_type / llm_event (news pipeline).

scorer      which scorer produced llm_score: 'llm' | 'local' | 'filter' (not about the company)
event_type  keyword-rule hard event for the headline (downgrade, offering, ...) or NULL
llm_event   hard event label from the LLM ('none' or an event key) or NULL
"""

from __future__ import annotations

from ..schema import add_column, table_columns

COLUMNS = {"scorer": "TEXT", "event_type": "TEXT", "llm_event": "TEXT"}


def upgrade(conn) -> None:
    have = table_columns(conn, "news_items")
    for col, kind in COLUMNS.items():
        if col not in have:
            add_column(conn, "news_items", col, kind)
