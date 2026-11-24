"""Entry point shim: ``python -m app.main`` and ``uvicorn app.main:app``.

The control plane lives in ``app/api`` (see ``app/api/app.py::create_app``).
"""

from __future__ import annotations

from .api.app import create_app, lifespan, log  # noqa: F401
from .config import DATA_DIR, load_settings

app = create_app()


def _acquire_lock() -> None:
    """Create a lock file with PID and port to prevent multiple instances.
    Cross-platform: works on Windows, macOS, and Linux.
    Exits with code 3 if another instance is running.
    """
    from .platform import acquire_single_instance_lock

    s = load_settings()
    lock_file = DATA_DIR / "trader.lock"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Acquire lock - exits with code 3 on duplicate
    lock = acquire_single_instance_lock(lock_file, s.port, s.host)

    # Store lock in app state for release on shutdown
    import atexit
    atexit.register(lock.release)


def run() -> None:
    import uvicorn

    # Single-instance guard: acquire lock before starting
    _acquire_lock()

    s = load_settings()
    if s.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("HOST must be 127.0.0.1: this app has no login and can place trades.")
    server = uvicorn.Server(uvicorn.Config(app, host=s.host, port=s.port, log_level="info"))
    app.state.server = server
    server.run()


if __name__ == "__main__":
    run()
