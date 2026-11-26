"""Promotion check: pass, fail, too-few-days; API gating."""
from __future__ import annotations

import numpy as np

from app.promotion import promotion_check


def _series(n, shadow_drift, target_drift, shadow_vol=0.005, target_vol=0.01):
    rng = np.random.default_rng(1)
    days = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)]
    s, t = 100.0, 100.0
    sh, tg = [], []
    for d in days:
        sh.append({"day": d, "mom": s})
        tg.append({"day": d, "swing": t})
        s *= 1 + rng.normal(shadow_drift, shadow_vol)
        t *= 1 + rng.normal(target_drift, target_vol)
    return sh, tg


def test_pass():
    sh, tg = _series(80, 0.003, 0.0005)
    r = promotion_check("mom", "swing", sh, tg)
    assert r["passed"] and r["days"] == 80
    assert {c["id"] for c in r["criteria"]} == {"days", "sharpe", "drawdown", "correlation"}


def test_too_few_days():
    sh, tg = _series(12, 0.003, 0.0005)
    r = promotion_check("mom", "swing", sh, tg)
    assert not r["passed"] and "12 of 60" in r["verdict"]


def test_fail_sharpe_and_drawdown():
    sh, tg = _series(80, -0.002, 0.002)
    r = promotion_check("mom", "swing", sh, tg)
    assert not r["passed"]
    by = {c["id"]: c for c in r["criteria"]}
    assert not by["sharpe"]["pass"] and by["days"]["pass"]


def test_correlation_not_a_gate():
    sh, tg = _series(80, 0.003, 0.0005)
    r = promotion_check("mom", "swing", sh, tg)
    c = next(c for c in r["criteria"] if c["id"] == "correlation")
    assert c["gate"] is False


def test_only_overlapping_days_count():
    sh, tg = _series(80, 0.003, 0.0005)
    r = promotion_check("mom", "swing", sh[:30], tg)
    assert r["days"] == 30 and not r["passed"]
