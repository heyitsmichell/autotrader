"""0003: at most one open (pending / submitting) proposal per symbol + side + namespace.

Existing duplicates are resolved first so the unique index can be created: the newest
row stays (an in-flight "submitting" row outranks a "pending" one); older "pending"
duplicates become "dismissed" ("duplicate (migration)"). An older duplicate that is
itself "submitting" may have reached the broker, so it becomes "unknown" and the
reconcile step settles it instead of the migration hiding it.
"""

from __future__ import annotations

INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_proposals_open ON proposals("
    "symbol, side, COALESCE(broker_mode, ''), COALESCE(webull_environment, ''), "
    "COALESCE(account_id, '')) WHERE status IN ('pending', 'submitting')"
)

REASON = "duplicate (migration)"


def upgrade(conn) -> None:
    rows = conn.execute(
        "SELECT id, symbol, side, status, COALESCE(broker_mode,'') AS bm, "
        "COALESCE(webull_environment,'') AS env, COALESCE(account_id,'') AS acct "
        "FROM proposals WHERE status IN ('pending','submitting') ORDER BY id").fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["symbol"], r["side"], r["bm"], r["env"], r["acct"]), []).append(r)
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = max(members, key=lambda r: (r["status"] == "submitting", r["id"]))
        for r in members:
            if r["id"] == keep["id"]:
                continue
            new = "dismissed" if r["status"] == "pending" else "unknown"
            conn.execute("UPDATE proposals SET status=?, reason=? WHERE id=?",
                         (new, REASON, r["id"]))
    conn.execute(INDEX)
