"""Shadow-to-live promotion endpoints. Never places orders: a successful POST only records a request."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..market import now_utc
from ..promotion import check_from_db
from ..shadows import registry
from ..sleeves import registry as sleeve_registry
from .deps import engine

router = APIRouter()


class PromoteBody(BaseModel):
    target: str
    capital_usd: float
    confirm: str


def _validate(shadow_id: str, target: str) -> None:
    try:
        registry.get(shadow_id)
    except KeyError as e:
        raise HTTPException(404, f"unknown shadow {shadow_id!r}") from e
    if target not in {s.id for s in sleeve_registry.SLEEVES}:
        raise HTTPException(404, f"unknown live sleeve {target!r}")


@router.get("/api/promotion/{shadow_id}")
def promotion_get(request: Request, shadow_id: str, target: str = "swing"):
    _validate(shadow_id, target)
    return check_from_db(engine(request).db, shadow_id, target)


@router.post("/api/promotion/{shadow_id}")
def promotion_post(request: Request, shadow_id: str, body: PromoteBody):
    _validate(shadow_id, body.target)
    if body.confirm != "PROMOTE":
        raise HTTPException(409, "confirmation text must be PROMOTE")
    if not body.capital_usd > 0:
        raise HTTPException(422, "capital_usd must be positive")
    db = engine(request).db
    check = check_from_db(db, shadow_id, body.target)
    if not check["passed"]:
        raise HTTPException(409, check["verdict"])
    req = {"shadow": shadow_id, "target": body.target, "capital_usd": body.capital_usd,
           "requested_at": now_utc().isoformat(timespec="seconds"), "check": check}
    db.set_kv("promotion_request", req)
    db.log("warn", f"PROMOTION REQUEST: shadow {shadow_id} -> live (vs {body.target}), ${body.capital_usd:,.0f}. "
                   "No orders were placed: a code change and deploy is needed to turn it into a live sleeve "
                   "(sleeves are defined in app/sleeves/registry.py).", alert=True)
    return {"ok": True, "request": req}
