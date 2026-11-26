"""ML model status, scorecard and training controls."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..db import decode
from .deps import background

router = APIRouter()


@router.get("/api/ml")
def ml_status(request: Request):
    st = request.app.state
    return {**st.ml.status(), "models": [decode(m) for m in st.db.models(20)],
            "retrain_days": st.settings.ml_retrain_days}


@router.get("/api/ml/{model_id}/scorecard")
def ml_scorecard(request: Request, model_id: int):
    """Get ML model scorecard with gate-check metrics and feature importance."""
    model = decode(request.app.state.db.get_model_by_id(model_id))
    if model is None:
        raise HTTPException(404, f"Model #{model_id} not found")
    metrics = model.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    return {
        # Walk-forward training stores mean_hit_rate / mean_auc; fall back to them so a trained
        # model never shows a blank scorecard.
        "precision": metrics.get("precision", metrics.get("mean_hit_rate")),
        "recall": metrics.get("recall"),
        "f1": metrics.get("f1"),
        "calibration_error": metrics.get("calibration_error"),
        "auc": metrics.get("auc", metrics.get("mean_auc")),
        "test_trades": metrics.get("total_test_trades"),
        "saturation_pct": metrics.get("saturation_pct"),
        "feature_importance": metrics.get("feature_importance", []),
        # Walk-forward results, passed through as stored (None when the model has none).
        "hit_rate": metrics.get("mean_hit_rate"),
        "auc_mean_folds": metrics.get("mean_auc"),
        "mean_avg_return": metrics.get("mean_avg_return"),
        "total_test_trades": metrics.get("total_test_trades"),
        "total_independent_test_trades": metrics.get("total_independent_test_trades"),
        "passed": metrics.get("passed"),
        "rejection_reason": metrics.get("rejection_reason"),
        "checks": metrics.get("checks") or {},
        "thresholds": metrics.get("thresholds") or {},
    }


@router.post("/api/ml/train")
async def ml_train(request: Request):
    st = request.app.state
    if not st.ml.available:
        raise HTTPException(409, st.ml.status()["error"] or "PyTorch is not available")
    if st.ml.training:
        raise HTTPException(409, "Training is already running")
    if not st.broker.connected:
        raise HTTPException(409, "Broker is not connected")

    async def go():
        try:
            await st.ml.train(st.broker, st.settings.watchlist)
            await st.signals.refresh(with_llm=False)
        except Exception:  # noqa: BLE001 - already logged
            pass

    background(request, go())
    return {"started": True}


@router.post("/api/ml/promote")
async def ml_promote(request: Request):
    """Make the model in shadow evaluation the champion (used when ML_AUTO_PROMOTE=false)."""
    st = request.app.state
    try:
        res = st.ml.promote_challenger()
    except ValueError as e:
        raise HTTPException(409, str(e))
    await st.signals.refresh(with_llm=False)
    return res


@router.post("/api/ml/reject")
async def ml_reject(request: Request):
    """Reject the model in shadow evaluation so a new one can be trained."""
    try:
        return request.app.state.ml.reject_challenger()
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/api/ml/deactivate")
async def ml_deactivate(request: Request):
    request.app.state.ml.deactivate()
    return {"ok": True}
