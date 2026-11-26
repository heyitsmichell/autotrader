"""Strategy / lessons text, lesson workflow and the learning loop endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..broker import BrokerError
from ..config import LESSONS_FILE, STRATEGY_FILE
from ..strategy_docs import read_lessons, read_strategy
from .deps import background
from .models import LessonOut, LessonRollbackIn, LessonStatusIn, TextIn

router = APIRouter()


@router.get("/api/lessons", response_model=list[LessonOut])
def lessons(request: Request, status: str | None = None, param_name: str | None = None,
                  limit: int = 50):
    return request.app.state.db.lessons(status, param_name, max(1, min(int(limit), 200)))


@router.post("/api/lessons/{lesson_id}/status")
async def lesson_status(request: Request, lesson_id: int, body: LessonStatusIn):
    """Change lesson status (proposed -> approved -> active -> retired, or proposed -> rejected)."""
    db = request.app.state.db
    lesson = db.get_lesson(lesson_id)
    if lesson is None:
        raise HTTPException(404, f"Lesson #{lesson_id} not found")
    valid_transitions = {
        "proposed": ["approved", "rejected"],
        "approved": ["active"],
        "active": ["retired"],
    }
    if body.status not in valid_transitions.get(lesson["status"], []):
        raise HTTPException(400, f"Invalid transition from {lesson['status']} to {body.status}")
    if body.status == "approved" and not body.approved_by:
        raise HTTPException(400, "approved_by required for approval")
    db.set_lesson_status(lesson_id, body.status, approved_by=body.approved_by)
    return {"ok": True, "status": body.status}


@router.post("/api/lessons/{lesson_id}/rollback")
async def lesson_rollback(request: Request, lesson_id: int, body: LessonRollbackIn):
    """Rollback lesson to previous version."""
    db = request.app.state.db
    try:
        new_id = db.rollback_lesson(lesson_id, body.rolled_back_by)
    except ValueError as e:
        raise HTTPException(409, f"Cannot rollback: {e}") from e
    if new_id is None:
        raise HTTPException(409, "Cannot rollback: no previous version or lesson not found")
    return {"ok": True, "rolled_back_to": new_id}


@router.get("/api/strategy")
async def get_strategy():
    return {"text": read_strategy()}


def _nonempty(text: str, what: str) -> str:
    clean = (text or "").strip()
    if not clean:
        raise HTTPException(400, f"{what} text must not be empty")
    return clean


@router.put("/api/strategy")
async def put_strategy(request: Request, body: TextIn):
    clean = _nonempty(body.text, "strategy")
    STRATEGY_FILE.write_text(clean + "\n", encoding="utf-8")
    request.app.state.db.log("info", "Strategy updated")
    return {"ok": True}


@router.put("/api/lessons")
async def put_lessons(request: Request, body: TextIn):
    clean = _nonempty(body.text, "lessons")
    LESSONS_FILE.write_text(clean + "\n", encoding="utf-8")
    request.app.state.db.log("info", "Lessons edited by you")
    return {"ok": True}


@router.get("/api/learning")
def learning(request: Request):
    st = request.app.state
    learner = st.learner
    s = st.settings
    reviews = st.db.reviews(1)
    return {
        "stats": learner.stats(),
        "analytics": learner.analytics(),
        "last_review": reviews[0] if reviews else None,
        "reviewing": learner.reviewing,
        "min_outcomes": s.min_outcomes_for_review,
        "review_every_days": s.review_every_days,
        "lessons": read_lessons(),
    }


@router.post("/api/learning/evaluate")
async def learning_evaluate(request: Request):
    try:
        n = await request.app.state.learner.evaluate_outcomes()
    except BrokerError as e:
        raise HTTPException(502, str(e)) from e
    return {"updated": n}


@router.post("/api/learning/review")
async def learning_review(request: Request):
    learner = request.app.state.learner
    if learner.reviewing:
        raise HTTPException(409, "A review is already running")
    n = learner.stats()["all"]["n"]
    if n < request.app.state.settings.min_outcomes_for_review:
        raise HTTPException(409, f"Need at least {request.app.state.settings.min_outcomes_for_review} "
                                 f"scored proposals for a review (have {n}).")
    engine = request.app.state.engine

    async def go():
        try:
            await learner.run_review("manual")
            # After review completes, refresh lessons in engine
            try:
                changes = engine.refresh_lessons()
                if changes > 0:
                    engine.db.log("info", f"Refreshed {changes} lesson parameter(s) after review")
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - already logged to the activity feed
            pass

    background(request, go())
    return {"started": True}


@router.post("/api/learning/refresh")
async def learning_refresh(request: Request):
    """Manually refresh engine settings from active lessons."""
    engine = request.app.state.engine
    changes = engine.refresh_lessons()
    return {"applied": changes, "sl_atr_mult": engine.s.sl_atr_mult,
            "tp_atr_mult": engine.s.tp_atr_mult,
            "consensus_ta_min": engine.s.consensus_ta_min,
            "ml_min_p": engine.s.ml_min_p}
