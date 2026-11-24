"""LLM per-request timeout and overall deadline (L2)."""
import asyncio
import time
from dataclasses import replace

import httpx
import pytest

from app import llm
from app.config import load_settings


class SlowClient:
    def __init__(self, delay=0.0, status=200):
        self.delay, self.status, self.calls, self.timeouts = delay, status, 0, []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        await asyncio.sleep(self.delay)
        return httpx.Response(self.status, json={"choices": [{"message": {"content": "{}"}}]},
                              headers={"retry-after": "30"} if self.status == 429 else {},
                              request=httpx.Request("POST", url))


def _s(**kw):
    llm.reset_groq_cooldown()
    base = dict(groq_api_key="k", nvidia_nim_api_key="n", llm_timeout_seconds=5.0,
                llm_deadline_seconds=1.0)
    base.update(kw)
    return replace(load_settings(), **base)


@pytest.mark.asyncio
async def test_overall_deadline_cuts_off_slow_provider():
    c = SlowClient(delay=5.0)
    t0 = time.monotonic()
    with pytest.raises(llm.LLMError, match="deadline"):
        await llm.chat_completion(_s(), messages=[{"role": "user", "content": "x"}], client=c)
    assert time.monotonic() - t0 < 2.0


@pytest.mark.asyncio
async def test_retry_sleep_respects_deadline():
    c = SlowClient(status=429)                       # Retry-After 30s would blow the deadline
    t0 = time.monotonic()
    with pytest.raises(llm.LLMError):
        await llm.chat_completion(_s(), messages=[{"role": "user", "content": "x"}], client=c)
    assert time.monotonic() - t0 < 2.0
    llm.reset_groq_cooldown()


@pytest.mark.asyncio
async def test_per_request_timeout_is_passed_and_bounded():
    c = SlowClient()
    s = _s(llm_timeout_seconds=7.0, llm_deadline_seconds=60.0)
    r = await llm.chat_completion(s, messages=[{"role": "user", "content": "x"}], client=c)
    assert r["_provider"] == "Groq" and 0 < c.timeouts[0] <= 7.0
