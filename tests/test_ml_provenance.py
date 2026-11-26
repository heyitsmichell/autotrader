"""Tests for ML provenance guards."""

import pytest

# QA H9: skip (instead of aborting collection) when requirements-ml.txt is not installed.
torch = pytest.importorskip("torch")


class TestMLProvenance:
    """Test ML provenance guards."""

    @pytest.mark.asyncio
    async def test_training_refused_in_mock_mode(self, test_settings, mock_broker, test_db):
        """Training should be refused in mock mode."""
        from app.ml import MLFilter

        ml = MLFilter(test_settings, test_db)
        try:
            await ml.train(mock_broker, test_settings.watchlist)
            assert False, "training should be refused in mock mode"
        except RuntimeError as e:
            assert "mock mode" in str(e).lower()

    @pytest.mark.asyncio
    async def test_model_with_mock_provenance_rejected_in_webull_mode(self, test_settings, mock_broker, test_db):
        """Model with mock provenance should be rejected in webull mode."""
        from app.ml import MLFilter, _net, FEATURES
        from app.config import MODEL_DIR
        import dataclasses

        if not MLFilter.available:
            pytest.skip("PyTorch not available")

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        mock_ckpt_path = MODEL_DIR / "model-mock-test.pt"
        model = _net(len(FEATURES))
        torch.save({
            "state": model.state_dict(),
            "mean": torch.zeros(len(FEATURES)).tolist(),
            "std": torch.ones(len(FEATURES)).tolist(),
            "features": FEATURES,
            "provenance": {"broker_mode": "mock", "webull_environment": None, "account_id": None}
        }, mock_ckpt_path)

        test_db.add_model(status="active", path=str(mock_ckpt_path), n_samples=100, n_symbols=3,
                          metrics={}, params={}, note="test",
                          broker_mode="mock", webull_environment=None, account_id=None)

        # Create MLFilter with webull settings to test rejection
        webull_settings = dataclasses.replace(test_settings, broker_mode="webull", webull_environment="uat")
        ml_webull = MLFilter(webull_settings, test_db)

        assert ml_webull.model is None
        assert "provenance mismatch" in (ml_webull.last_error or "").lower()

        mock_ckpt_path.unlink(missing_ok=True)
        test_db.set_model_status(test_db.active_model()["id"], "retired")

    @pytest.mark.asyncio
    async def test_leakage_saturation_guards(self, test_settings, test_db):
        """Leakage/saturation guards should exist."""
        from app.ml import walk_forward

        wf_result = walk_forward([], test_settings.consensus_ta_min, test_settings.ml_min_p, min_auc=test_settings.ml_min_auc)
        assert "rejection_note" in wf_result
        assert "max_auc" in wf_result["thresholds"]
        assert "saturation_threshold" in wf_result["thresholds"]
        assert "min_symbols" in wf_result["thresholds"]
        assert "no_leakage" in wf_result["checks"]
        assert "no_saturation" in wf_result["checks"]
        assert "min_symbols" in wf_result["checks"]

    @pytest.mark.asyncio
    async def test_ml_min_bars_config_setting(self, test_settings):
        """ML_MIN_BARS config setting should exist."""
        assert hasattr(test_settings, "ml_min_bars")
        assert test_settings.ml_min_bars == 500

    @pytest.mark.asyncio
    async def test_research_service_effective_watchlist(self, test_settings, mock_broker, test_db):
        """ResearchService.effective_watchlist() should work."""
        from app.research import ResearchService

        research = ResearchService(test_settings, mock_broker, test_db)
        assert hasattr(research, "effective_watchlist")
        ewl = research.effective_watchlist()
        assert isinstance(ewl, list)
        assert len(ewl) >= len(test_settings.watchlist)
        assert all(sym in ewl for sym in test_settings.watchlist)

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_sine_wave_data_not_promoted(self, test_settings, test_db):
        """Sine-wave data should NOT promote a model."""
        from app.ml import MLFilter, build_dataset, walk_forward
        from app import indicators
        import math
        from datetime import datetime, timedelta

        if not MLFilter.available:
            pytest.skip("PyTorch not available")

        # Create synthetic sine-wave bars (like the mock broker uses)
        sine_bars = []
        for i in range(500):
            # Sum of sine waves with different periods (like mock_mcp_server.py)
            price = 100 + 5 * math.sin(i * 0.1) + 2 * math.sin(i * 0.05) + 1 * math.sin(i * 0.02)
            sine_bars.append({
                "time": datetime(2020, 1, 1) + timedelta(days=i),
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 1000000
            })
        bars_by_symbol = {"SINE": sine_bars}
        rows = build_dataset(bars_by_symbol)
        wf = walk_forward(rows, test_settings.consensus_ta_min, test_settings.ml_min_p)

        assert wf["passed"] is False
        assert "rejection_note" in wf