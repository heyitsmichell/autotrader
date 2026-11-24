"""Accessors for objects stored on ``request.app.state`` (set by the lifespan)."""

from __future__ import annotations

import asyncio

from fastapi import Request

from ..market import start_of_today_et_utc


def state(request: Request):
    return request.app.state


def engine(request: Request):
    return request.app.state.engine


def background(request: Request, coro) -> None:
    """Run a coroutine as a tracked background task (kept alive until done)."""
    task = asyncio.create_task(coro)
    tasks = request.app.state.tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def today_start() -> str:
    return start_of_today_et_utc().isoformat(timespec="seconds")
