"""Read the human-written strategy and lessons notes (strategy.md, lessons.md).

These files are context for the learning reviewer and for the dashboard. They do NOT drive live
trading decisions: every trading rule is enforced deterministically in consensus.py, risk.py,
eligibility.py and exits.py. (They used to feed the LLM agent in app/agent.py, which was never
invoked and has been removed.)
"""

from __future__ import annotations

from .config import LESSONS_FILE, STRATEGY_FILE


def read_strategy() -> str:
    try:
        return STRATEGY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def read_lessons() -> str:
    try:
        return LESSONS_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
