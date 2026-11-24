"""OpenAI-compatible LLM client with Groq primary and optional NVIDIA NIM fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
NVIDIA_NIM_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
GROQ_COOLDOWN_SECONDS = 60.0
_groq_cooldown_until = 0.0


def groq_cooldown_remaining() -> float:
    """Return remaining cooldown seconds for Groq (0.0 if not in cooldown)."""
    return max(0.0, _groq_cooldown_until - time.monotonic())


def reset_groq_cooldown() -> None:
    """Reset the Groq rate-limit cooldown timer (useful for tests)."""
    global _groq_cooldown_until
    _groq_cooldown_until = 0.0


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 retryable: bool = False, retry_delay: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_delay = retry_delay
        self.authentication = status_code in {401, 403} or "API_KEY" in message


def _retry_delay(response: httpx.Response) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.1, float(retry_after))
        except ValueError:
            pass
    try:
        message = response.json().get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        message = ""
    match = re.search(r"try again in\s+([\d.]+)s", message, re.I)
    return max(0.1, float(match.group(1))) if match else 2.0


def _remaining(deadline_at: float | None) -> float:
    return float("inf") if deadline_at is None else deadline_at - time.monotonic()


async def _post_completion(session: httpx.AsyncClient, *, url: str, key: str,
                           provider: str, payload: dict[str, Any], retries: int,
                           timeout: float | None = None,
                           deadline_at: float | None = None) -> dict:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(retries + 1):
        left = _remaining(deadline_at)
        if left <= 0:
            raise LLMError(f"{provider}: overall LLM deadline exceeded")
        try:
            # Per-request timeout, never longer than what is left of the overall deadline.
            per_request = min(timeout, left) if timeout is not None else None
            kwargs = {"timeout": per_request} if per_request is not None else {}
            response = await session.post(url, headers=headers, json=payload, **kwargs)
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach {provider}: {exc}", retryable=True,
                           retry_delay=2.0) from exc
        retryable = response.status_code == 429 or response.status_code >= 500
        if retryable and attempt < retries:
            delay = min(_retry_delay(response), 60.0)
            if delay < _remaining(deadline_at):      # a sleep past the deadline is pointless
                await asyncio.sleep(delay)
                continue
        if response.is_error:
            try:
                body = response.json()
                detail = body.get("error", {}).get("message") or response.text
            except (ValueError, AttributeError):
                detail = response.text
            raise LLMError(
                f"{provider} API returned HTTP {response.status_code}: {str(detail)[:700]}",
                status_code=response.status_code, retryable=retryable,
                retry_delay=_retry_delay(response) if retryable else None)
        try:
            return response.json()
        except ValueError as exc:
            raise LLMError(f"{provider} returned a response that was not valid JSON") from exc
    raise LLMError(f"{provider} request failed without a response")


async def chat_completion(settings: Settings, *, messages: list[dict],
                          tools: list[dict] | None = None,
                          json_schema: tuple[str, dict] | None = None,
                          max_tokens: int = 2048,
                          client: httpx.AsyncClient | None = None) -> dict:
    """Call Groq, failing over to NVIDIA NIM for transient primary-provider errors.

    Each HTTP request is bounded by ``llm_timeout_seconds`` and the whole call (retries, sleeps
    and provider fallbacks) by ``llm_deadline_seconds`` (L2); exceeding it raises LLMError.
    """
    deadline = float(getattr(settings, "llm_deadline_seconds", 90.0))
    try:
        return await asyncio.wait_for(
            _chat_completion(settings, messages=messages, tools=tools, json_schema=json_schema,
                             max_tokens=max_tokens, client=client,
                             deadline_at=time.monotonic() + deadline),
            deadline)
    except asyncio.TimeoutError:
        raise LLMError(f"LLM call exceeded the {deadline:g}s overall deadline") from None


async def _chat_completion(settings: Settings, *, messages: list[dict],
                           tools: list[dict] | None, json_schema: tuple[str, dict] | None,
                           max_tokens: int, client: httpx.AsyncClient | None,
                           deadline_at: float) -> dict:
    timeout = float(getattr(settings, "llm_timeout_seconds", 25.0))
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if json_schema:
        name, schema = json_schema
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        }
    session = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
    owns_session = client is None
    failures: list[tuple[str, LLMError]] = []

    async def call(route_model: str, provider: str, url: str, key: str,
                   route_payload: dict[str, Any], retries: int = 0) -> dict:
        response = await _post_completion(
            session, url=url, key=key, provider=provider,
            payload=route_payload, retries=retries, timeout=timeout, deadline_at=deadline_at)
        response["_provider"] = provider
        response["_provider_model"] = response.get("model") or route_model
        return response

    global _groq_cooldown_until
    groq_available = bool(settings.groq_api_key and time.monotonic() >= _groq_cooldown_until)
    try:
        if groq_available:
            try:
                return await call(settings.llm_model, "Groq", GROQ_CHAT_URL,
                                  settings.groq_api_key, payload)
            except LLMError as primary_error:
                failures.append(("Groq primary", primary_error))
                if primary_error.status_code == 429:
                    cooldown = max(GROQ_COOLDOWN_SECONDS, primary_error.retry_delay or 0.0)
                    _groq_cooldown_until = time.monotonic() + cooldown
                    groq_available = False
                    log.info("Groq primary rate-limited (HTTP 429); cooldown set for %.1fs, falling back", cooldown)
        else:
            remaining = _groq_cooldown_until - time.monotonic()
            failures.append(("Groq primary", LLMError(
                f"Groq temporarily skipped in rate-limit cooldown ({remaining:.1f}s remaining)"
                if settings.groq_api_key else "GROQ_API_KEY is missing from .env")))

        # A different Groq model often has its own model-specific token bucket.
        # This gives runs a same-provider path before using the external fallback.
        if settings.groq_fallback_model != settings.llm_model and groq_available:
            try:
                response = await call(
                    settings.groq_fallback_model, "Groq fallback", GROQ_CHAT_URL,
                    settings.groq_api_key,
                    {**payload, "model": settings.groq_fallback_model})
                return response
            except LLMError as err:
                failures.append(("Groq alternate model", err))
                if err.status_code == 429:
                    cooldown = max(GROQ_COOLDOWN_SECONDS, err.retry_delay or 0.0)
                    _groq_cooldown_until = time.monotonic() + cooldown
                    groq_available = False
                    log.info("Groq fallback rate-limited (HTTP 429); cooldown set for %.1fs", cooldown)

        primary_error = failures[0][1]
        if settings.nvidia_nim_api_key:
            fallback_payload = {**payload, "model": settings.nvidia_nim_model}
            # Nemotron's endpoint defaults to streaming and substantial internal
            # reasoning. This client expects a single JSON response, so bound both.
            fallback_payload.update({
                "stream": False,
                "temperature": 1,
                "top_p": 0.95,
                "reasoning_budget": min(512, max_tokens // 4),
            })
            try:
                return await call(
                    settings.nvidia_nim_model, "NVIDIA NIM", NVIDIA_NIM_CHAT_URL,
                    settings.nvidia_nim_api_key, fallback_payload, retries=1)
            except LLMError as err:
                failures.append(("NVIDIA NIM", err))

        # Honor Groq's Retry-After after trying independent routes. The window may
        # have reset while those providers were attempted, allowing the primary to recover.
        delay = primary_error.retry_delay
        if (settings.groq_api_key and time.monotonic() >= _groq_cooldown_until
                and primary_error.retryable and delay is not None):
            wait = max(0.1, min(delay, 60.0))
            if wait < _remaining(deadline_at):       # no point sleeping past the deadline
                await asyncio.sleep(wait)
                try:
                    return await call(settings.llm_model, "Groq", GROQ_CHAT_URL,
                                      settings.groq_api_key, payload)
                except LLMError as err:
                    failures.append(("Groq retry", err))

        details = "; ".join(f"{name}: {err}" for name, err in failures)
        last = failures[-1][1]
        raise LLMError(f"All configured LLM routes failed: {details}",
                       status_code=last.status_code, retryable=last.retryable,
                       retry_delay=last.retry_delay) from last
    finally:
        if owns_session:
            await session.aclose()


def provider_model(response: dict, default_model: str) -> str:
    """Provider-returned model ID, falling back to the configured primary model."""
    return str(response.get("_provider_model") or response.get("model") or default_model)


def message_text(response: dict) -> str:
    choices = response.get("choices") or []
    if not choices:
        raise LLMError("LLM provider returned no completion choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def usage_counts(response: dict) -> tuple[int, int, int]:
    usage = response.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    return prompt, completion, cached


def tool_calls(response: dict) -> list[dict]:
    choices = response.get("choices") or []
    if not choices:
        return []
    return (choices[0].get("message") or {}).get("tool_calls") or []


def assistant_message(response: dict) -> dict:
    choices = response.get("choices") or []
    if not choices:
        raise LLMError("LLM provider returned no completion choices")
    message = choices[0].get("message") or {"role": "assistant", "content": ""}
    # Keep only portable assistant fields. NVIDIA reasoning metadata is not part of
    # the standard messages shape and can make the next Groq request fail validation.
    return {key: message[key] for key in ("role", "content", "tool_calls", "function_call", "name")
            if key in message}


def parse_json_response(response: dict) -> dict:
    try:
        value = json.loads(message_text(response))
    except (ValueError, TypeError) as exc:
        raise LLMError("LLM provider returned invalid JSON for a structured response") from exc
    if not isinstance(value, dict):
        raise LLMError("LLM structured response must be a JSON object")
    return value
