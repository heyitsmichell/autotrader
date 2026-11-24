"""Dashboard cost estimate."""

from __future__ import annotations

# USD per million tokens: Groq's free-tier quota is $0. This is a dashboard
# estimate only; provider account usage and quota remain authoritative.
PRICING = {"openai/gpt-oss-120b": (0.0, 0.0, 0.0, 0.0)}


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int,
                  cache_read: int = 0, cache_write: int = 0) -> float | None:
    """Estimated USD for one call. input_tokens = all input, including cached."""
    price = PRICING.get(model or "")
    if price is None:
        return None
    p_in, p_out, p_read, p_write = price
    fresh = max(0, input_tokens - cache_read - cache_write)
    return round((fresh * p_in + output_tokens * p_out + cache_read * p_read
                  + cache_write * p_write) / 1_000_000, 4)

