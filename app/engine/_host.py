"""Typing-only declarations of the host attributes the Engine mixins rely on.

Each ``*Host`` Protocol lists what that mixin reads or sets on ``self`` without defining it
itself (``CoreMixin.__init__`` or a sibling mixin provides it). Mixins inherit the matching
Protocol under ``TYPE_CHECKING`` only, so there is no runtime effect.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Protocol


class _Base(Protocol):
    s: Any                       # Settings
    db: Any                      # DB
    broker: Any                  # Broker
    kill_switch: bool


class CoreHost(Protocol):
    s: Any
    db: Any
    broker: Any
    signals: Any
    portfolio: Any
    risk: Any
    option_risk: Any
    research: Any
    alerts: Any
    signal_store: Any
    trading_mode: str
    kill_switch: bool
    kill_reason: str
    kill_at: Any
    run_failures: int
    order_failures: int
    last_failure: Any
    scheduler_stopped_by_user: bool
    flattening: bool
    next_run_at: Any
    on_order: Any
    _current_run: Any
    _run_lock: asyncio.Lock
    _exec_lock: asyncio.Lock
    _proposal_lock: asyncio.Lock
    _cycle_active: bool
    _scheduler: Any
    _last_llm_run: float
    _min_llm_cycle_seconds: float
    def _init_runtime(self) -> None: ...
    def stop_scheduler(self) -> Awaitable[None]: ...


class CycleHost(_Base, Protocol):
    signals: Any
    research: Any
    signal_store: Any
    run_failures: int
    _cycle_active: bool
    _read_fail_logged_at: Any
    _last_llm_run: float
    _min_llm_cycle_seconds: float
    _run_lock: asyncio.Lock
    def _failed(self, kind: str, message: str) -> None: ...
    def _auto_execute_run(self, run_id: int) -> Awaitable[None]: ...
    def save_run_params(self, run_id: int) -> None: ...
    def _generate_proposals_deterministic(self, run_id: int, sigs: list[dict]) -> Awaitable[int]: ...


class ExecutionHost(_Base, Protocol):
    research: Any
    portfolio: Any
    option_risk: Any
    risk: Any
    trading_mode: str
    kill_reason: str
    order_failures: int
    on_order: Any
    _exec_lock: asyncio.Lock
    def _failed(self, kind: str, message: str) -> None: ...


class FlattenHost(_Base, Protocol):
    flattening: bool
    order_failures: int
    on_order: Any
    option_exits: Any
    _exec_lock: asyncio.Lock
    def set_kill_switch(self, on: bool, reason: str = ...) -> Awaitable[None]: ...


class OptionEntriesHost(_Base, Protocol):
    signals: Any
    option_exits: Any
    __dict__: dict[str, Any]
    def _on_proposal(self, run_id: int, args: dict, sig_override: dict | None = None) -> Awaitable[str]: ...


class OptionsFlowHost(_Base, Protocol):
    alerts: Any
    trading_mode: str
    _atm_iv_day: Any
    _atm_iv_attempt: Any


class ProposalsHost(_Base, Protocol):
    research: Any
    alerts: Any
    signals: Any
    signal_store: Any
    risk: Any
    option_risk: Any
    trading_mode: str
    kill_reason: str
    _proposal_lock: asyncio.Lock
    def _is_opening(self, p: dict) -> bool: ...
    def _option_consensus_gate(self, p: dict) -> Awaitable[dict]: ...
    def _create_option_proposal(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...
    def propose_option_entries(self, run_id: int, sigs: list[dict]) -> Awaitable[int]: ...
    def _satellite_headroom_usd(self) -> Awaitable[float]: ...


class RebalanceHost(_Base, Protocol):
    research: Any
    risk: Any
    trading_mode: str
    def _add_exit_row(self, *args: Any, **kwargs: Any) -> Any: ...
    def _send(self, pid: int, p: dict, actor: str, what: str) -> Awaitable[None]: ...


class ReconcileHost(_Base, Protocol):
    portfolio: Any
    option_exits: Any
    _exec_lock: asyncio.Lock
    __dict__: dict[str, Any]


class RiskLoopHost(Protocol):
    option_exits: Any
    risk_loop: Any
    workers: Any
    _atm_iv_day: Any
    _atm_iv_attempt: Any


class SchedulerHost(_Base, Protocol):
    scheduler_stopped_by_user: bool
    scheduler_on: Any
    next_run_at: Any
    _scheduler: Any
    _current_run: Any
    def run_cycle(self, trigger: str) -> Awaitable[int | None]: ...


class WorkersHost(Protocol):
    workers: Any


class EngineHost(CoreHost, CycleHost, ExecutionHost, FlattenHost, OptionEntriesHost,
                 OptionsFlowHost, ProposalsHost, RebalanceHost, ReconcileHost,
                 RiskLoopHost, SchedulerHost, WorkersHost, Protocol):
    """Everything the Engine mixins need from the composed ``Engine``."""
