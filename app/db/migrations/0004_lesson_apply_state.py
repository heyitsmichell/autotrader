"""0004: lessons.apply_state / apply_reason (M9) and the missing lessons.updated_at.

`set_lesson_status` has always written `updated_at`, but the column was never created.
"""

from __future__ import annotations

from ..schema import add_column, table_columns


def upgrade(conn) -> None:
    have = table_columns(conn, "lessons")
    if "updated_at" not in have:
        add_column(conn, "lessons", "updated_at", "TEXT")
    if "apply_state" not in have:
        add_column(conn, "lessons", "apply_state", "TEXT NOT NULL DEFAULT 'pending'")
    if "apply_reason" not in have:
        add_column(conn, "lessons", "apply_reason", "TEXT")
