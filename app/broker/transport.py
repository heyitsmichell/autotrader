"""MCP transport: subprocess lifecycle, ``call()``, rate limiting and the negative cache.

``McpTransport`` owns the stdio connection to the Webull MCP server (or the mock one).
Webull credentials go only to that process through its environment.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent, Tool

from ..config import LOG_DIR, ROOT
from ..logging_setup import trim_shared_logs
from .errors import BrokerError
from .parsers import (ERROR_PREFIXES, is_rate_limited, retry_after_seconds, sanitize,
                      strip_disclaimer)

log = logging.getLogger(__name__)

# Tools that change broker state. They are never served from, or written to, the negative
# cache (L1): a cached 404 on cancel_order must not block a later legitimate retry, and a
# failed place must never be answered with a stale error.
MUTATING_PREFIXES = ("place_", "cancel_", "replace_", "modify_", "submit_", "close_")


NEGATIVE_CACHE_MAX = 512
_NOT_FOUND_RX = re.compile(r"\bHTTP 404\b|\b404 Not Found\b|\bNot Found\b")


def is_mutating_tool(name: str) -> bool:
    return name.startswith(MUTATING_PREFIXES)


# Minimum seconds between two calls of the same read tool (client-side token bucket with one
# token per interval). Concurrent workers used to fire bursts, hit 429 and then all sleep.
DEFAULT_MIN_INTERVALS = {"get_stock_bars": 0.35, "get_account_positions": 1.0}



def sleeve_symbols(settings) -> list[str]:
    """Symbols the multi-strategy sleeves may trade, so the Webull MCP server's symbol whitelist
    accepts them: the S&P 500 list (momentum), the VAA ETFs and the put-write underlying.
    Empty when sleeves are off. (2026-11-25: momentum/VAA/put-write orders were refused with
    'symbol is not in the allowed whitelist' because only the swing universe was listed.)"""
    if not getattr(settings, "sleeves_enabled", False):
        return []
    out: list[str] = []
    try:
        if getattr(settings, "sleeve_momentum_enabled", True):
            from ..sleeves.data import sp500_symbols
            out += sp500_symbols()
        if getattr(settings, "sleeve_vaa_enabled", True):
            from ..sleeves.strategies import vaa
            out += list(vaa.G12) + list(vaa.DEFENSIVE)
        if getattr(settings, "sleeve_putwrite_enabled", True):
            out.append(str(getattr(settings, "sleeve_putwrite_underlying", "QQQ")).upper())
    except Exception:  # noqa: BLE001 - never block broker start-up
        log.warning("could not build the sleeve symbol whitelist", exc_info=True)
    return [x.upper() for x in out if x]

class CallSpacer:
    """Spaces calls of one tool: each caller reserves the next free slot and sleeps until it."""

    def __init__(self, intervals: dict[str, float], clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.intervals, self._clock, self._sleep = dict(intervals), clock, sleep
        self._next: dict[str, float] = {}

    async def acquire(self, name: str) -> float:
        """Wait for this tool's next slot. Returns the seconds waited."""
        gap = self.intervals.get(name, 0.0)
        if gap <= 0:
            return 0.0
        now = self._clock()
        slot = max(now, self._next.get(name, 0.0))
        self._next[name] = slot + gap
        wait = slot - now
        if wait > 0:
            await self._sleep(wait)
        return max(wait, 0.0)


def _make_hashable(obj):
    if isinstance(obj, dict):
        return tuple(sorted((k, _make_hashable(v)) for k, v in obj.items()))
    if isinstance(obj, (list, tuple)):
        return tuple(_make_hashable(v) for v in obj)
    return obj


def _log_tail(path, offset: int) -> str:
    """Last meaningful line the server wrote to its log since `offset`."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(offset if offset <= os.fstat(f.fileno()).st_size else 0)
            lines = [ln.strip() for ln in f.read().splitlines()]
    except OSError:
        return ""
    lines = [ln for ln in lines if ln and not set(ln) <= set("=-")]
    return lines[-1].rstrip(".") if lines else ""


def _flatten(e: BaseException) -> str:
    """Readable message for (possibly nested) exception groups."""
    if isinstance(e, BaseExceptionGroup) and e.exceptions:
        return _flatten(e.exceptions[0])
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


class McpTransport:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.session: ClientSession | None = None
        self.tools: list[Tool] = []
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        # Negative cache for failed read endpoints (key -> retry_after_timestamp)
        self._negative_cache: dict[str, float] = {}
        self._negative_cache_ttl = 300
        # Rate limit handling
        self._rate_limit_retry_after: float = 0
        self._rate_limit_backoff_base = float(getattr(settings, "broker_backoff_seconds", 2.0))
        self._rate_limit_max_retries = int(getattr(settings, "broker_rate_limit_retries", 4))
        self._call_timeout = float(getattr(settings, "broker_call_timeout_seconds", 30.0))
        # Concurrency cap; created lazily so it binds to the running loop.
        self._max_concurrency = int(getattr(settings, "broker_max_concurrency", 4))
        self._call_sem: asyncio.Semaphore | None = None
        intervals = dict(DEFAULT_MIN_INTERVALS)
        if getattr(settings, "broker_mode", "") == "mock":
            intervals = {}
        self._spacer = CallSpacer(intervals)

    # -- lifecycle ---------------------------------------------------------
    def _server_params(self) -> StdioServerParameters:
        # Pass the environment through and fill in the guardrails the Webull server
        # enforces itself. Values set here win over its own read of .env.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ANTHROPIC_", "GROQ_"))}
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if self.settings.broker_mode == "mock":
            args = ["-m", "app.mock_mcp_server"]
        else:
            # Include option underlyings in the whitelist so option orders work. Also whitelist
            # the core symbol and every symbol in the wider universes merged into
            # settings.watchlist, otherwise the Webull server rejects names the engine allows.
            whitelist = list(dict.fromkeys(
                self.settings.watchlist + [self.settings.core_symbol]
                + self.settings.options_allowed_underlyings + sleeve_symbols(self.settings)))
            defaults = {
                "WEBULL_TOKEN_DIR": str(ROOT / "conf"),
                "WEBULL_AUDIT_LOG_FILE": str(LOG_DIR / "webull_audit.log"),
                "WEBULL_SYMBOL_WHITELIST": ",".join(whitelist),
                "WEBULL_MAX_ORDER_NOTIONAL_USD": str(self.settings.max_order_usd),
                "WEBULL_MAX_ORDER_QUANTITY": str(self.settings.max_order_qty),
            }
            for key, value in defaults.items():
                if not env.get(key):
                    env[key] = value
            args = ["-m", "webull_openapi_mcp", "serve"]
        return StdioServerParameters(command=self._mcp_python(), args=args, env=env, cwd=str(ROOT))

    def _mcp_python(self) -> str:
        """Interpreter for the MCP server.

        webull_openapi_mcp is installed only in the project .venv, so if the app was
        started with a different Python (e.g. a bare `python3 -m app.main`), use the
        venv's interpreter instead of failing with "No module named webull_openapi_mcp".
        """
        if self.settings.broker_mode == "mock":
            return sys.executable
        import importlib.util
        if importlib.util.find_spec("webull_openapi_mcp") is not None:
            return sys.executable
        for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
            cand = ROOT / rel
            if cand.exists():
                return str(cand)
        return sys.executable

    async def start(self, timeout: float = 90) -> None:
        await self.stop()
        self.error = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        trim_shared_logs()
        log_file = LOG_DIR / "mcp_server.log"
        log_offset = log_file.stat().st_size if log_file.exists() else 0
        self._stop = asyncio.Event()
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(ready, self._stop), name="mcp-connection")
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout)
        except asyncio.TimeoutError:
            await self.stop()
            self.error = (f"MCP server did not start within {timeout:.0f}s. "
                          f"{_log_tail(log_file, log_offset)}See logs/mcp_server.log")
            raise BrokerError(self.error) from None
        except Exception as e:
            await self.stop()
            self.error = (f"Could not start MCP server: {_log_tail(log_file, log_offset) or e}. "
                          "See logs/mcp_server.log")
            raise BrokerError(self.error) from e

    async def _run(self, ready: asyncio.Future, stop: asyncio.Event) -> None:
        """Owns the stdio connection for its whole life (anyio needs one task)."""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        session_initialized = False
        try:
            with open(LOG_DIR / "mcp_server.log", "a", encoding="utf-8") as errlog:
                async with stdio_client(self._server_params(), errlog=errlog) as (read, write):
                    # Note: read_timeout_seconds causes BrokenResourceError on cleanup in mcp 1.30.0
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self.tools = (await session.list_tools()).tools
                        self.session = session
                        session_initialized = True
                        if not ready.done():
                            ready.set_result(None)
                        await stop.wait()
        except BaseException as e:  # noqa: BLE001 - report every failure to start()
            # If the session was initialized, the exception is likely a cleanup error
            # (e.g. BrokenResourceError during stdio_client __aexit__), not a startup failure.
            if session_initialized and ready.done():
                if not stop.is_set():
                    self.error = f"MCP connection lost during cleanup: {_flatten(e)}"
                if isinstance(e, asyncio.CancelledError):
                    raise
                return
            if not ready.done():
                ready.set_exception(RuntimeError(_flatten(e)))
            elif not stop.is_set():
                self.error = f"MCP connection lost: {_flatten(e)}"
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self.session = None

    async def stop(self) -> None:
        if self._stop:
            self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 10)
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
                cancelled = isinstance(exc, asyncio.CancelledError)
                if not cancelled:
                    log.debug("transport task did not stop cleanly: %r", exc)
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass  # the task we just cancelled
                except Exception as exc2:  # noqa: BLE001
                    log.debug("transport task ended with error: %s", exc2)
                if cancelled:
                    self._task = None
                    self.session = None
                    raise
        self._task = None
        self.session = None
        # Give the subprocess a moment to fully terminate
        await asyncio.sleep(0.1)

    # -- calls -------------------------------------------------------------
    async def call(self, name: str, **arguments) -> str:
        # The negative cache only ever applies to read-only tools (L1).
        cacheable = not is_mutating_tool(name)
        cache_key = f"{name}:{hash(_make_hashable(arguments))}"
        if cacheable and cache_key in self._negative_cache:
            if time.time() < self._negative_cache[cache_key]:
                raise BrokerError(f"{name}: cached failure (negative cache active)")
            del self._negative_cache[cache_key]

        if self.session is None:
            raise BrokerError("Broker is not connected" + (f": {self.error}" if self.error else ""))

        if self._call_sem is None:
            self._call_sem = asyncio.Semaphore(max(1, self._max_concurrency))

        # `arguments` is reused verbatim on every retry, so a client_order_id supplied by the
        # caller is never regenerated.
        for attempt in range(self._rate_limit_max_retries + 1):
            # A 429 seen by any caller pauses every caller until the window passes.
            pause = self._rate_limit_retry_after - time.time()
            if pause > 0:
                await asyncio.sleep(pause)
            if not is_mutating_tool(name):
                await self._spacer.acquire(name)
            try:
                async with self._call_sem:
                    result = await asyncio.wait_for(
                        self.session.call_tool(name, arguments=arguments),
                        timeout=self._call_timeout or None)
            except asyncio.TimeoutError as e:
                # The call may still have taken effect at the broker. Never retried here (a
                # timeout is not a 429), so a mutating call is not sent twice; callers settle
                # the unknown outcome by client_order_id.
                raise BrokerError(f"{name} timed out after {self._call_timeout:g}s "
                                  "(outcome unknown)", transport=True) from e
            except Exception as e:  # closed connection, protocol errors
                flat = _flatten(e)
                if is_rate_limited(flat) and attempt < self._rate_limit_max_retries:
                    await self._rate_limit_wait(name, attempt, flat)
                    continue
                raise BrokerError(f"{name} failed: {flat}", transport=True) from e
            text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
            text = strip_disclaimer(sanitize(text))
            if getattr(result, "is_error", False) or text.startswith(ERROR_PREFIXES):
                if is_rate_limited(text):
                    if attempt < self._rate_limit_max_retries:
                        await self._rate_limit_wait(name, attempt, text)
                        continue
                    log.error(f"Rate limit exceeded for {name} after {self._rate_limit_max_retries + 1} attempts")
                if cacheable and _NOT_FOUND_RX.search(text):
                    while len(self._negative_cache) >= NEGATIVE_CACHE_MAX:
                        self._negative_cache.pop(next(iter(self._negative_cache)))  # oldest first
                    self._negative_cache[cache_key] = time.time() + self._negative_cache_ttl
                raise BrokerError(f"{name}: {text}")
            return text

        raise BrokerError(f"{name}: max retries exceeded")

    async def _rate_limit_wait(self, name: str, attempt: int, text: str) -> None:
        """Back off exponentially with jitter (or by the server's Retry-After)."""
        hinted = retry_after_seconds(text)
        backoff = hinted if hinted is not None else \
            self._rate_limit_backoff_base * (2 ** attempt) + random.uniform(0, 1)
        backoff = min(backoff, 60.0)
        # the first back-off is routine (~6 a minute on UAT) and always recovers: debug, not a warning
        (log.debug if attempt == 0 else log.warning)(
            f"Rate limited on {name} (attempt {attempt + 1}/{self._rate_limit_max_retries + 1}), "
            f"backing off {backoff:.1f}s")
        self._rate_limit_retry_after = max(self._rate_limit_retry_after, time.time() + backoff)
