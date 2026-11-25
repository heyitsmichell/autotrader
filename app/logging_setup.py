"""Process-wide logging setup (moved from app/main.py) plus secret redaction (H4).

``setup_logging()`` is idempotent. It
  * configures the root logger exactly as main.py used to (INFO, same format);
  * adds the rotating files: logs/app.log (5 MB), logs/mcp_server.log (1 MB, "mcp" logger; copytruncate via trim_log),
    logs/webull_audit.log (0.5 MB, "webull_audit" logger), 5 backups each;
  * sets ``httpx`` / ``httpcore`` to WARNING (their INFO lines print full request URLs, which for
    Telegram contain the bot token);
  * installs a redaction ``logging.Filter`` on every handler.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import shutil

from .config import LOG_DIR

log = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# (pattern, replacement). Order matters: specific shapes first, generic key=value last.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"bot\d+:[\w-]+"), "bot***"),                                   # telegram token
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer ***"),
    (re.compile(r"(?i)\bapi[_-]?key\s*[=:]\s*[^&\s,;\"']+"), "api_key=***"),
    (re.compile(r"(?i)\bapp[_-]?secret\s*[=:]\s*[^&\s,;\"']+"), "app_secret=***"),
    (re.compile(r"(?i)\b(token|secret|key)=[^&\s]+"), r"\1=***"),
]


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class RedactionFilter(logging.Filter):
    """Rewrites the formatted message and exception text so secrets never reach a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never lose a log line over a bad format string
            return True
        red = redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_redaction() -> None:
    """Attach the filter to every handler of the root logger and of the app's own loggers."""
    seen: set[int] = set()
    loggers = [logging.getLogger()] + [logging.getLogger(n) for n in ("mcp", "webull_audit")]
    for lg in loggers:
        for h in lg.handlers:
            if id(h) in seen:
                continue
            seen.add(id(h))
            if not any(isinstance(f, RedactionFilter) for f in h.filters):
                h.addFilter(RedactionFilter())


MCP_LOG_MAX = 1_000_000
AUDIT_LOG_MAX = 500_000


def trim_log(path, max_bytes: int, backups: int = 5) -> bool:
    """Copytruncate rotation; safe while another process holds `path` open in append mode.

    Shifts path.N backups (oldest dropped), copies the file to path.1, truncates in place.
    Never raises. Returns True if it rotated.
    """
    try:
        path = str(path)
        if os.path.getsize(path) <= max_bytes:
            return False
        for i in range(backups, 0, -1):
            src = path if i == 1 else f"{path}.{i - 1}"
            dst = f"{path}.{i}"
            if os.path.exists(src):
                if i == backups and os.path.exists(dst):
                    os.remove(dst)
                if i == 1:
                    shutil.copyfile(src, dst)
                else:
                    os.replace(src, dst)
        with open(path, "r+b") as f:
            f.truncate(0)
        return True
    except OSError as e:
        log.warning("log trim failed for %s: %s", path, e)
        return False


def trim_shared_logs() -> None:
    """Trim the two logs also written by the MCP subprocess."""
    trim_log(LOG_DIR / "mcp_server.log", MCP_LOG_MAX)
    trim_log(LOG_DIR / "webull_audit.log", AUDIT_LOG_MAX)


def _shared(name: str) -> logging.FileHandler:
    """Plain append handler (no rename rotation) for files another process also writes."""
    handler = logging.FileHandler(LOG_DIR / name, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def _rotating(name: str, max_bytes: int) -> logging.handlers.RotatingFileHandler:
    handler = logging.handlers.RotatingFileHandler(LOG_DIR / name, maxBytes=max_bytes,
                                                   backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def setup_logging(*, files: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if files:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
            trim_shared_logs()
            root.addHandler(_rotating("app.log", 5_000_000))
            mcp_logger = logging.getLogger("mcp")
            mcp_logger.addHandler(_shared("mcp_server.log"))
            mcp_logger.setLevel(logging.INFO)
            mcp_logger.propagate = False
            audit_logger = logging.getLogger("webull_audit")
            audit_logger.addHandler(_shared("webull_audit.log"))
            audit_logger.setLevel(logging.INFO)
            audit_logger.propagate = False
    install_redaction()
