#!/usr/bin/env python3
"""Migration script to add broker_mode, webull_environment, account_id columns to all tables
and tag existing rows based on timestamps and 'App started. Broker: ...' events.
Run this once after pulling the updated code."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import DATA_DIR  # noqa: E402

DB_PATH = DATA_DIR / "trader.db"
LOCK_FILE = DATA_DIR / "trader.lock"        # written by the app (app/main.py, app/platform.py)


def _pid_alive(pid: int) -> bool:
    """Is a process with this pid running? (os.kill(pid, 0) would TERMINATE it on Windows.)"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def app_is_running(lock_file: Path = LOCK_FILE) -> bool:
    """True when the app's single-instance lock file names a live process or a busy port."""
    try:
        text = lock_file.read_text().strip()
    except OSError:
        return False
    try:
        pid_s, _, port_s = text.partition(":")
        pid, port = int(pid_s), int(port_s) if port_s else 0
    except ValueError:
        return True            # unreadable lock: be conservative and refuse
    if _pid_alive(pid):
        return True
    if port:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
    return False

# Determine the current broker mode from the events table
def detect_mode_for_row(conn: sqlite3.Connection, table: str, row_id: int, created_at: str) -> tuple[str, str, str]:
    """Detect broker_mode, webull_environment, account_id for a row based on the nearest 'App started' event."""
    # Find the most recent 'App started' event before this row's created_at
    row = conn.execute(
        """SELECT message FROM events
           WHERE ts <= ? AND message LIKE 'App started. Broker:%'
           ORDER BY ts DESC LIMIT 1""",
        (created_at,)
    ).fetchone()

    if row:
        msg = row[0]
        # Parse: "App started. Broker: Webull US - Webull sandbox (uat)" or "Mock broker (simulated)"
        if "Mock broker" in msg:
            return "mock", None, None
        elif "Webull sandbox" in msg or "uat" in msg.lower():
            # Extract account ID if present in any later event
            acct_row = conn.execute(
                """SELECT message FROM events
                   WHERE ts <= ? AND message LIKE '%account_id%'
                   ORDER BY ts DESC LIMIT 1""",
                (created_at,)
            ).fetchone()
            account_id = None
            if acct_row:
                # Try to extract account ID from message
                import re
                m = re.search(r'account[_\s]+id[:\s]+(\S+)', acct_row[0], re.I)
                if m:
                    account_id = m.group(1)
            return "webull", "uat", account_id
        elif "LIVE" in msg or "prod" in msg.lower():
            acct_row = conn.execute(
                """SELECT message FROM events
                   WHERE ts <= ? AND message LIKE '%account_id%'
                   ORDER BY ts DESC LIMIT 1""",
                (created_at,)
            ).fetchone()
            account_id = None
            if acct_row:
                import re
                m = re.search(r'account[_\s]+id[:\s]+(\S+)', acct_row[0], re.I)
                if m:
                    account_id = m.group(1)
            return "webull", "prod", account_id

    # Default: if we can't determine, use the first 'App started' event in the DB
    first_start = conn.execute(
        "SELECT message FROM events WHERE message LIKE 'App started. Broker:%' ORDER BY ts ASC LIMIT 1"
    ).fetchone()
    if first_start:
        msg = first_start[0]
        if "Mock broker" in msg:
            return "mock", None, None
        elif "Webull sandbox" in msg or "uat" in msg.lower():
            return "webull", "uat", None
        elif "LIVE" in msg or "prod" in msg.lower():
            return "webull", "prod", None

    # Ultimate fallback
    return "unknown", None, None


def main(argv: list[str] | None = None) -> int:
    global DB_PATH
    ap = argparse.ArgumentParser(description="Tag existing rows with broker/environment/account.")
    ap.add_argument("--db", type=Path, default=DB_PATH, help=f"database file (default {DB_PATH})")
    args = ap.parse_args(argv)
    DB_PATH = args.db
    if app_is_running(LOCK_FILE):
        print(f"Refusing to run: the app is running (lock file {LOCK_FILE}). Stop it first.",
              file=sys.stderr)
        return 2
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1
    print(f"Backing up {DB_PATH}...")
    import datetime
    backup = DB_PATH.with_suffix(f".backup-pre-namespace-{datetime.datetime.now():%Y%m%d-%H%M%S}.db")
    src = sqlite3.connect(DB_PATH, timeout=5)
    dst = sqlite3.connect(backup)
    try:
        src.backup(dst)           # consistent copy, includes anything still in the WAL
    finally:
        dst.close()
        src.close()
    print(f"Backup created: {backup}")

    # Schema first: the numbered migrations (app/db/migrations) add the namespace columns
    # and everything else. Opening DB runs them; no ALTER statements live in this script.
    from app.db import DB
    DB(DB_PATH).close()

    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row

    # Tables that need namespacing (excluding models which already has the columns)
    tables = [
        "runs", "proposals", "signals", "news_items", "equity_snapshots", "protective_orders",
        "llm_usage", "reviews", "events", "research_packets",
    ]
    for table in tables:
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_namespace "
                     f"ON {table}(broker_mode, webull_environment, account_id)")
    conn.commit()

    # Column name for timestamp varies by table
    ts_column = {
        "runs": "started_at",
        "proposals": "created_at",
        "signals": "created_at",
        "news_items": "fetched_at",
        "equity_snapshots": "ts",
        "protective_orders": "created_at",
        "llm_usage": "ts",
        "reviews": "created_at",
        "events": "ts",
        "research_packets": "created_at",
    }

    # Now tag existing rows
    print("\n--- Tagging existing rows ---")

    for table in tables:
        print(f"\nTagging rows in {table}...")
        timestamp_col = ts_column.get(table, "created_at")

        # Special handling for news_items (composite PK)
        if table == "news_items":
            rows = conn.execute(f"SELECT news_id, symbol, {timestamp_col} FROM {table} WHERE broker_mode IS NULL").fetchall()
            if not rows:
                print(f"  No rows to tag (or already tagged)")
                continue
            for row in rows:
                news_id = row["news_id"]
                symbol = row["symbol"]
                created_at = row[timestamp_col]
                broker_mode, webull_env, account_id = detect_mode_for_row(conn, table, f"{news_id}:{symbol}", created_at)
                conn.execute(
                    f"UPDATE {table} SET broker_mode=?, webull_environment=?, account_id=? WHERE news_id=? AND symbol=?",
                    (broker_mode, webull_env, account_id, news_id, symbol)
                )
        else:
            rows = conn.execute(f"SELECT id, {timestamp_col} FROM {table} WHERE broker_mode IS NULL").fetchall()
            if not rows:
                print(f"  No rows to tag (or already tagged)")
                continue

            for row in rows:
                row_id = row["id"]
                created_at = row[timestamp_col]
                broker_mode, webull_env, account_id = detect_mode_for_row(conn, table, row_id, created_at)
                conn.execute(
                    f"UPDATE {table} SET broker_mode=?, webull_environment=?, account_id=? WHERE id=?",
                    (broker_mode, webull_env, account_id, row_id)
                )

        tagged = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE broker_mode IS NOT NULL").fetchone()[0]
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  Tagged {tagged}/{total} rows")

    # Namespace kv keys
    print("\nNamespacing kv keys...")
    kv_rows = conn.execute("SELECT key, value FROM kv WHERE namespace IS NULL").fetchall()
    for row in kv_rows:
        key = row["key"]
        value = row["value"]
        # Determine namespace from key content or default to unknown
        namespace = "unknown"
        if key in ("kill_switch", "equity_peak", "daily_loss_ack", "research_last_refresh", "research_auto_watchlist"):
            # These are global-ish, but we'll tag them with the first known mode
            first_mode = conn.execute(
                "SELECT broker_mode FROM events WHERE broker_mode IS NOT NULL ORDER BY ts ASC LIMIT 1"
            ).fetchone()
            if first_mode:
                namespace = first_mode[0]
            else:
                namespace = "mock"  # Default assumption for old data
        new_key = f"{namespace}:{key}"
        conn.execute("UPDATE kv SET key=?, namespace=? WHERE key=?", (new_key, namespace, key))
        print(f"  {key} -> {new_key}")

    # Re-base equity peak: for each namespace, compute peak from equity_snapshots
    print("\nRe-basing equity peaks per namespace...")
    namespaces = conn.execute(
        "SELECT DISTINCT broker_mode, webull_environment, account_id FROM equity_snapshots WHERE broker_mode IS NOT NULL"
    ).fetchall()
    for ns in namespaces:
        broker_mode, webull_env, account_id = ns
        ns_key = f"{broker_mode}:{webull_env or 'none'}:{account_id or 'none'}"
        peak_row = conn.execute(
            """SELECT MAX(net_liq) FROM equity_snapshots
               WHERE broker_mode=? AND webull_environment IS ? AND account_id IS ?""",
            (broker_mode, webull_env, account_id)
        ).fetchone()
        if peak_row and peak_row[0] is not None:
            peak = peak_row[0]
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, namespace) VALUES (?, ?, ?)",
                (f"{ns_key}:equity_peak", json.dumps(peak), f"{broker_mode}:{webull_env or 'none'}:{account_id or 'none'}")
            )
            print(f"  {ns_key} peak = {peak}")

    conn.commit()
    conn.close()
    print("\nMigration complete!")
    print(f"Backup saved at: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())