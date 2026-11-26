"""Local API auth (M21): loopback Host + ``X-Trader: 1`` + per-launch ``X-Trader-Token``.

The token is generated once per launch, written to ``settings.api_token_file`` (0600 on POSIX)
and injected into served HTML as ``<meta name="trader-token" content="...">``.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import sys
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("trader")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
TOKEN_ERROR = {"detail": "missing or invalid X-Trader-Token"}
_FRAME_HEADERS = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_token_file(path: Path, token: str) -> None:
    """Write the token to ``path``, readable only by the current user where the OS supports it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path.write_text(token, encoding="utf-8")   # best effort: inherits the per-user data dir ACL
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def request_hostname(request: Request) -> str:
    host = (request.headers.get("host") or "").lower()
    return host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]


def token_ok(request: Request) -> bool:
    expected = getattr(request.app.state, "api_token", None)
    given = request.headers.get("x-trader-token") or ""
    if not expected or not given:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def install_auth(app: FastAPI, trusted_hosts: list[str] | None = None) -> None:
    """``trusted_hosts`` (TRUSTED_HOSTS) are extra Host names allowed besides loopback, e.g. the
    server's private Tailscale name. Token and X-Trader checks apply to them unchanged."""
    allowed = LOOPBACK_HOSTS | {h.lower() for h in (trusted_hosts or []) if h}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request_hostname(request)
        if host not in allowed:
            return JSONResponse({"detail": "forbidden host"}, status_code=403, headers=_FRAME_HEADERS)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.url.path.startswith("/api/"):
                if request.headers.get("x-trader") != "1" or not token_ok(request):
                    return JSONResponse(TOKEN_ERROR, status_code=403, headers=_FRAME_HEADERS)
            elif request.headers.get("x-trader") != "1":
                return JSONResponse({"detail": "missing X-Trader header"}, status_code=403,
                                    headers=_FRAME_HEADERS)
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return resp


router = APIRouter()

_LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}
_LOCAL_HOST_NAMES = {"localhost", "127.0.0.1", "::1"}


def dev_token_endpoint_allowed(request: Request) -> bool:
    """Dev only: env TRADER_DEV_TOKEN_ENDPOINT=1, or a loopback client addressing localhost
    directly. Behind ``tailscale serve`` the client is loopback but Host is the tailnet name."""
    if os.environ.get("TRADER_DEV_TOKEN_ENDPOINT") == "1":
        return True
    client = request.client.host if request.client else ""
    return client in _LOOPBACK_CLIENTS and request_hostname(request) in _LOCAL_HOST_NAMES


@router.get("/api/auth/token")
async def auth_token(request: Request):
    """Dev-mode fallback (the served HTML already carries the token). The custom header forces
    a CORS preflight, which fails cross-origin because no CORS is configured."""
    if not dev_token_endpoint_allowed(request):
        raise HTTPException(404, "Not Found")
    if request.headers.get("x-trader") != "1":
        raise HTTPException(403, "missing X-Trader header")
    return {"token": request.app.state.api_token}
