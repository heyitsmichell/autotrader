"""0001: create every baseline table and upgrade drifted databases in place.

This is the former ad-hoc constructor logic: CREATE TABLE IF NOT EXISTS, add any column
SCHEMA / ADDED_COLUMNS declare but an old file lacks, namespace columns, and the copy of
data from the pre-rename provider-specific columns. Idempotent on any existing database.
"""

from __future__ import annotations

from ..schema import (ADDED_COLUMNS, SCHEMA, add_column, ensure_namespace_columns,
                      sync_schema_columns, table_columns)

# executescript() issues its own COMMIT, so this one cannot run inside a transaction.
NO_TRANSACTION = True


def upgrade(conn) -> None:
    conn.executescript(SCHEMA)
    sync_schema_columns(conn)
    for table, columns in ADDED_COLUMNS.items():
        have = table_columns(conn, table)
        for col, kind in columns.items():
            if col not in have:
                add_column(conn, table, col, kind)
    # Preserve sentiment already scored by the previous provider-specific schema.
    if "sent_claude" in table_columns(conn, "signals"):
        conn.execute("UPDATE signals SET sent_llm=sent_claude WHERE sent_llm IS NULL")
    news_cols = table_columns(conn, "news_items")
    if "claude_score" in news_cols:
        conn.execute("UPDATE news_items SET llm_score=claude_score WHERE llm_score IS NULL")
        conn.execute("UPDATE news_items SET llm_relevance=claude_relevance "
                     "WHERE llm_relevance IS NULL")
        conn.execute("UPDATE news_items SET llm_label=claude_label WHERE llm_label IS NULL")
        conn.execute("UPDATE news_items SET llm_reason=claude_reason WHERE llm_reason IS NULL")
    ensure_namespace_columns(conn)
