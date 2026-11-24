"""Process-wide logging setup (moved from app/main.py) plus secret redaction (H4).

``setup_logging()`` is idempotent. It
  * configures the root logger exactly as main.py used to (INFO, same format);
  * adds the rotating files: logs/app.log (5 MB), logs/mcp_server.log (1 MB, "mcp" logger),
    logs/webull_audit.log (0.5 MB, "webull_audit" logger), 5 backups each;
  * sets ``httpx`` / ``httpcore`` to WARNING (their INFO lines print full request URLs, which for
    Telegram contain the bot token);
  * installs a redaction ``logging.Filter`` on every handler.
"""

from __future__ import annotations

import logging
import logging.handlers
import re

from .config import LOG_DIR

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
            root.addHandler(_rotating("app.log", 5_000_000))
            mcp_logger = logging.getLogger("mcp")
            mcp_logger.addHandler(_rotating("mcp_server.log", 1_000_000))
            mcp_logger.setLevel(logging.INFO)
            mcp_logger.propagate = False
            audit_logger = logging.getLogger("webull_audit")
            audit_logger.addHandler(_rotating("webull_audit.log", 500_000))
            audit_logger.setLevel(logging.INFO)
            audit_logger.propagate = False
    install_redaction()
