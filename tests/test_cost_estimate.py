"""Tests for cost estimation."""

import pytest


class TestCostEstimate:
    """Test cost estimation."""

    def test_groq_free_tier_model_estimated_cost(self, test_settings):
        """Groq free-tier model estimated cost should be 0."""
        from app.config import estimate_cost
        c = estimate_cost("openai/gpt-oss-120b", 1_000_000, 100_000, cache_read=500_000)
        assert c == 0.0