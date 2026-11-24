#!/usr/bin/env python3
"""
SQLite database backup script using SQLite's backup API for consistent backups.

Creates timestamped backups in the same directory as the source database (or in --dest,
for example another disk). Each backup is verified with PRAGMA integrity_check and the
script exits non-zero when the result is not "ok" (the bad copy is removed). Keeps only the
last N backups (default 10).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


class BackupIntegrityError(RuntimeError):
    """The backup copy failed PRAGMA integrity_check."""


def verify_backup(path: Path) -> str:
    """Run PRAGMA integrity_check on a database file; return its first result row."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "no result"
    finally:
        conn.close()


def backup_database(db_path: Path, backup_dir: Path | None = None, keep: int = 10) -> Path:
    """Create a consistent backup of the SQLite database using the backup API.

    Args:
        db_path: Path to the source database file
        backup_dir: Directory to store backups (defaults to db_path.parent)
        keep: Number of recent backups to keep (older ones are deleted)

    Returns:
        Path to the created backup file
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    backup_dir = backup_dir or db_path.parent
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_name = f"{db_path.stem}.backup-{timestamp}{db_path.suffix}"
    backup_path = backup_dir / backup_name

    # Use SQLite's backup API for consistent backup (handles active connections)
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dest = sqlite3.connect(backup_path)

    try:
        source.backup(dest, pages=0)  # pages=0 means copy all at once
    finally:
        dest.close()
        source.close()

    result = verify_backup(backup_path)
    if result != "ok":
        backup_path.unlink(missing_ok=True)
        raise BackupIntegrityError(f"integrity_check on the backup returned {result!r}")
    log.info(f"Backup created and verified: {backup_path}")

    # Clean up old backups
    _prune_old_backups(backup_dir, db_path.stem, keep)

    return backup_path


def _prune_old_backups(backup_dir: Path, db_stem: str, keep: int) -> None:
    """Delete backups older than the most recent `keep` backups."""
    # Only the .db files count as backups; -wal/-shm sidecars must not eat into `keep`.
    backups = sorted(backup_dir.glob(f"{db_stem}.backup-*.db"))
    if len(backups) > keep:
        for old in backups[:-keep]:
            try:
                old.unlink()
                for side in ("-wal", "-shm"):
                    Path(f"{old}{side}").unlink(missing_ok=True)
                log.info(f"Removed old backup: {old}")
            except Exception as e:
                log.warning(f"Failed to remove {old}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup SQLite database")
    parser.add_argument("db_path", help="Path to the database file")
    parser.add_argument("--backup-dir", "--dest", dest="backup_dir",
                        help="Directory for backups, e.g. on another disk (default: same as db)")
    parser.add_argument("--keep", type=int, default=10, help="Number of backups to keep")
    args = parser.parse_args()

    try:
        backup_database(Path(args.db_path), Path(args.backup_dir) if args.backup_dir else None, args.keep)
        return 0
    except Exception as e:
        log.error(f"Backup failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())