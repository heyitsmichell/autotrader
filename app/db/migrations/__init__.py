"""Numbered schema migrations, tracked with ``PRAGMA user_version``.

Each module ``NNNN_name.py`` defines ``upgrade(conn)`` and must be idempotent (safe on a
database that already has the change). They run in numeric order inside the DB
constructor, before the first write. ``user_version`` is bumped after each one succeeds.
A migration that cannot run inside a transaction sets ``NO_TRANSACTION = True``.
A database whose version is higher than the newest migration (a newer app wrote it) is
left alone.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from types import ModuleType

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+$")


def discover() -> list[tuple[int, str, ModuleType]]:
    found = []
    for mod in pkgutil.iter_modules(__path__):                 # type: ignore[name-defined]
        m = _NAME.match(mod.name)
        if m:
            found.append((int(m.group(1)), mod.name,
                          importlib.import_module(f"{__name__}.{mod.name}")))
    found.sort(key=lambda t: t[0])
    nums = [n for n, _, _ in found]
    if len(set(nums)) != len(nums):
        raise RuntimeError(f"duplicate migration numbers: {nums}")
    return found


def latest_version() -> int:
    found = discover()
    return found[-1][0] if found else 0


def current_version(conn) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def run_migrations(conn) -> int:
    """Apply every pending migration in order; return the resulting user_version."""
    with conn.lock:
        version = current_version(conn)
        for number, _name, module in discover():
            if number <= version:
                continue
            if getattr(module, "NO_TRANSACTION", False):
                # e.g. executescript() (implicit COMMIT), VACUUM, journal_mode: not wrappable.
                module.upgrade(conn)
                conn.execute(f"PRAGMA user_version={number}")
            else:
                with conn.transaction():
                    module.upgrade(conn)
                    conn.execute(f"PRAGMA user_version={number}")
            version = number
        return version
