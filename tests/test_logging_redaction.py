"""Secret redaction on every log handler, httpx silenced (H4)."""
import io
import logging
from dataclasses import replace

import httpx
import pytest

from app.logging_setup import RedactionFilter, redact, setup_logging

TOKEN = "123456789:AAH-secret_token-xyz"


@pytest.mark.parametrize("raw, gone", [
    (f"POST https://api.telegram.org/bot{TOKEN}/sendMessage", TOKEN),
    ("Authorization: Bearer abc.def-123", "abc.def-123"),
    ("GET /x?api_key=SUPERSECRET&y=1", "SUPERSECRET"),
    ("app_secret: HUSH", "HUSH"),
    ("url?token=tok123&a=b secret=s3 key=k4", "tok123"),
])
def test_redact_patterns(raw, gone):
    out = redact(raw)
    assert gone not in out and "***" in out


def test_filter_redacts_args_and_exceptions():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.addFilter(RedactionFilter())
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("trader.redact_test")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.info("sending to %s", f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    try:
        raise RuntimeError(f"boom bot{TOKEN}")
    except RuntimeError:
        lg.exception("failed")
    assert TOKEN not in buf.getvalue() and "bot***" in buf.getvalue()


def test_setup_logging_silences_httpx_and_filters_all_handlers(monkeypatch, tmp_path):
    monkeypatch.setattr("app.logging_setup.LOG_DIR", tmp_path)
    root = logging.getLogger()
    before = list(root.handlers)
    saved = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).propagate)
             for n in ("mcp", "webull_audit")}
    try:
        setup_logging()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert root.handlers and all(any(isinstance(f, RedactionFilter) for f in h.filters)
                                     for h in root.handlers)
        # A simulated httpx log line carrying the telegram URL: INFO is dropped by the level, and
        # even a WARNING that includes the URL is redacted by the file handler filter.
        url = httpx.Request("POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage").url
        logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"', url)
        logging.getLogger("httpx").warning("HTTP Request: POST %s", url)
        for h in root.handlers:
            h.flush()
        text = (tmp_path / "app.log").read_text(encoding="utf-8")
        assert TOKEN not in text and "bot***" in text and text.count("HTTP Request") == 1
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        for n, (hs, prop) in saved.items():
            lg = logging.getLogger(n)
            for h in lg.handlers[:]:
                if h not in hs:
                    lg.removeHandler(h)
                    h.close()
            lg.propagate = prop


async def _noop(*a, **k):
    return None


@pytest.mark.asyncio
async def test_alerts_do_not_leak_token(monkeypatch, caplog):
    from app.alerts import Alerts
    from app.config import load_settings

    s = replace(load_settings(), alert_telegram_token=TOKEN, alert_telegram_chat_id="1")
    a = Alerts(s, db=None)

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr("app.alerts.httpx.AsyncClient", Boom)
    monkeypatch.setattr("app.alerts.SEND_BACKOFF", (0.0, 0.0))   # no real retry sleeps
    with pytest.raises(RuntimeError) as ei:
        await a._telegram("t", "b", "info")
    assert TOKEN not in str(ei.value)
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(a, "_local", _noop)
    res = await a.send("hello")
    assert TOKEN not in str(res) and TOKEN not in caplog.text
