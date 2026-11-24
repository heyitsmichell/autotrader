"""Pydantic response models. All are permissive (extra keys kept, fields optional) so that
adding a response key never breaks a client and a sparse row never causes a 500."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class DailyLossGuard(_Loose):
    enabled: bool = False
    tripped: bool = False
    pnl: float | None = None
    limit: float | None = None


class StatusOut(_Loose):
    broker_label: str | None = None
    broker_mode: str | None = None
    is_live: bool = False
    connected: bool = False
    broker_error: str | None = None
    trading_mode: str | None = None
    allow_live_auto: bool = False
    kill_switch: bool = False
    kill_reason: str | None = None
    scheduler_on: bool = False
    next_run_at: str | None = None
    running: bool = False
    market_open: bool = False
    now_et: str | None = None
    server_time: str | None = None
    watchlist: list[str] = []
    day_pnl: float | None = None
    trades_today: int | None = None
    flattening: bool = False
    # contract §7 additions
    data_age_seconds: float | None = None
    broker_mode_warning: str | None = None
    paused: bool = False
    daily_loss_guard: DailyLossGuard = DailyLossGuard()
    token_required: bool = True


class ProposalOut(_Loose):
    id: int | None = None
    created_at: str | None = None
    symbol: str | None = None
    side: str | None = None
    quantity: float | None = None
    limit_price: float | None = None
    confidence: float | None = None
    rationale: Any = None
    status: str | None = None
    reason: str | None = None


class LessonOut(_Loose):
    id: int | None = None
    version: int | None = None
    param_name: str | None = None
    param_value: float | None = None
    param_scope: str | None = None
    status: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    apply_state: str | None = None
    apply_reason: str | None = None


class TradeRow(_Loose):
    """One trade_ledger row. The same shape is returned for status=open|closed|all (L11)."""
    id: int | None = None
    proposal_id: int | None = None
    symbol: str | None = None
    side: str | None = None
    quantity_requested: float | None = None
    quantity_filled: float | None = None
    limit_price: float | None = None
    entry_fill_price: float | None = None
    entry_fill_time: str | None = None
    entry_slippage: float | None = None
    entry_commission: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    exit_fill_price: float | None = None
    exit_fill_time: str | None = None
    exit_reason: str | None = None
    exit_slippage: float | None = None
    exit_commission: float | None = None
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    mae: float | None = None
    mfe: float | None = None
    hold_seconds: float | None = None
    entry_price: float | None = None
    entry_atr: float | None = None
    entry_trend: str | None = None
    entry_ta_score: float | None = None
    entry_sent_score: float | None = None
    entry_ml_p: float | None = None
    entry_features: Any = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    broker_mode: str | None = None
    webull_environment: str | None = None
    account_id: str | None = None
    quantity_requested_real: float | None = None
    quantity_filled_real: float | None = None
    entrust_type: str | None = None
    total_cash_amount: float | None = None


class ModeIn(BaseModel):
    mode: str
    confirm: str = ""


class ToggleIn(BaseModel):
    on: bool


class KillIn(BaseModel):
    on: bool
    confirm: str = ""


class TextIn(BaseModel):
    text: str


class RefreshIn(BaseModel):
    with_llm: bool = False


class FlattenIn(BaseModel):
    confirm: str = ""


class LessonStatusIn(BaseModel):
    status: str
    approved_by: str | None = None


class LessonRollbackIn(BaseModel):
    rolled_back_by: str
