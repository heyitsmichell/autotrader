"""Backup and shutdown."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .base import BaseRepo


class MaintenanceRepo(BaseRepo):
    """Mixin for the DB facade: backup() and close()."""

    def backup(self, suffix: str | None = None) -> Path:
        """Create a timestamped backup of the database file."""
        from datetime import datetime
        if suffix is None:
            suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
        backup_path = Path(backup_path).with_suffix(f".backup-{suffix}.db")
        # Use SQLite backup API for a consistent snapshot
        dest = sqlite3.connect(backup_path)
        try:
            with dest:
                self.conn.backup(dest)
        finally:
            dest.close()
        return backup_path

    def close(self) -> None:
        """Checkpoint the WAL (TRUNCATE) and close the connection. Idempotent."""
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
