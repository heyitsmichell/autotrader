"""Engine state: init, lessons auto-apply, mode, kill switch."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime

from ..broker import Broker
from ..config import Settings
from ..db import DB
from ..eventhub import hub
from ..market import now_utc
from ..risk import OptionRiskManager, RiskManager
from ..signal_store import SignalStore
from ..signals import SignalService

log = logging.getLogger("app.engine")

# Parameters that lessons may change automatically, with their allowed bounds.
AUTO_APPLICABLE = {
    "sl_atr_mult": (0.5, 5.0),          # stop loss ATR multiplier
    "tp_atr_mult": (0.5, 10.0),         # take profit ATR multiplier
    "consensus_ta_min": (-0.5, 0.8),    # TA threshold
    "ml_min_p": (0.4, 0.8),             # ML probability threshold
    "consensus_sent_min": (-0.5, 0.0),  # sentiment floor
    "consensus_min_news": (0, 10),      # minimum news articles
    "price_band_pct": (0.1, 5.0),       # price band percentage
}

# Parameters that must NEVER be auto-applied (risk limits).
NEVER_AUTO_APPLY = {
    "max_order_usd", "max_position_usd", "max_order_qty",
    "max_trades_per_day", "max_daily_loss_usd", "max_risk_per_trade_usd",
    "kill_daily_loss_usd", "kill_action",
}

# Extra settings recorded with each run besides AUTO_APPLICABLE (M9 provenance).
RUN_PARAM_GATES = (
    "trading_mode", "ml_enabled", "ml_first", "ml_first_top_n", "ml_first_ta_floor",
    "bracket_orders", "fractional_enabled", "sizing_mode",
    "max_order_usd", "max_position_usd", "max_order_qty", "max_trades_per_day",
    "max_daily_loss_usd", "max_risk_per_trade_usd", "kill_action",
    "options_enabled", "options_iv_rank_max", "options_iv_rank_min", "options_delta_min",
    "options_delta_max", "options_min_volume", "options_min_open_interest",
    "options_max_bid_ask_pct", "options_quote_max_age_seconds", "options_exit_stop_pct",
    "options_exit_take_profit_pct", "options_exit_dte", "options_expiry_close_time",
    "entry_pullback_gate", "max_adds_per_symbol", "block_add_to_loser",
    "exit_max_hold_days", "exit_confirm_readings", "core_enabled",
)


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing-only host contract; no runtime base class
    from ._host import CoreHost as _Host
else:
    _Host = object

class CoreMixin(_Host):
    def __init__(self, settings: Settings, db: DB, broker: Broker,
                 signals: SignalService, portfolio=None, alerts=None) -> None:
        self.s = settings
        self.db = db
        self.broker = broker
        self.signals = signals
        self.portfolio = portfolio
        self.risk = RiskManager(settings)
        self.option_risk = OptionRiskManager(settings)
        self.trading_mode = settings.trading_mode
        self.research = None
        self.alerts = alerts
        if self.trading_mode == "auto" and settings.is_live and not settings.allow_live_auto:
            self.trading_mode = "approve"
            self.db.log("error", "Configured live auto-trading is disabled; forced approval mode. "
                                 "Set ALLOW_LIVE_AUTO_TRADING=true only after reviewing live risks.")
        # The kill switch survives restarts: a crash-and-restart must never resume trading
        # that you (or an automatic trigger) stopped.
        ks = db.get_kv("kill_switch") or {}
        self.kill_switch = bool(ks.get("on"))
        self.kill_reason: str | None = ks.get("reason") if self.kill_switch else None
        self.kill_at: str | None = ks.get("at") if self.kill_switch else None
        # Consecutive failures, read by the supervisor's automatic kill switch.
        self.run_failures = 0
        self.order_failures = 0
        self.last_failure: str | None = None
        self.scheduler_stopped_by_user = False
        self.flattening = False
        self._current_run: asyncio.Task | None = None
        self.next_run_at: datetime | None = None
        self._run_lock = asyncio.Lock()
        self._exec_lock = asyncio.Lock()
        # M3: every _on_proposal (slow cycle and fast loop) runs under this lock.
        self._proposal_lock = asyncio.Lock()
        self._cycle_active = False
        self._scheduler: asyncio.Task | None = None
        self.on_order = None   # callback after an order is placed (refreshes portfolio cache)
        self._proposal_attempts = {}
        self._last_llm_run: datetime | None = None
        self._min_llm_cycle_seconds = settings.llm_min_cycle_minutes * 60  # LLM_MIN_CYCLE_MINUTES
        # M4: cold-path signals land here; the slow cycle and the fast loop only read it.
        refresh_s = 60 * max(1, int(getattr(settings, "signal_refresh_minutes", 15) or 15))
        self.signal_store = SignalStore(ttl_seconds=max(120.0, 2.5 * refresh_s))
        try:
            signals.store = self.signal_store
        except AttributeError:
            pass
        self._init_runtime()
        # M1/M2: the broker refuses opening orders while the kill switch is on, whichever path
        # the order comes from. Closing orders and flatten_mode() are exempt there.
        try:
            broker.set_guard_context(kill_switch=lambda: self.kill_switch, db=db)
        except AttributeError:  # test doubles without the guard API
            log.debug("broker has no set_guard_context")
        # Multi-strategy sleeves: shares held by a sleeve are hidden from every swing code path
        # (broker.positions() subtracts them). Harmless when no sleeve holds anything.
        if hasattr(db, "sleeve_reserved"):
            try:
                broker.__dict__["reserved_positions"] = db.sleeve_reserved
            except (AttributeError, TypeError):  # slotted test doubles
                log.debug("broker cannot take reserved_positions")
        # Apply active lessons with guardrails
        self._apply_active_lessons()

    # -- lessons (M9) ----------------------------------------------------------
    def _apply_active_lessons(self) -> None:
        """Apply statistically validated lessons from the database to settings.

        Guardrails:
        - Only lessons with status='active'
        - Only lessons with sample_size >= min_sample_size (default 30)
        - Only lessons with p_value < 0.05 (statistically significant)
        - Only allow certain parameters to be auto-applied (never risk limits)

        Every lesson that is looked at ends up recorded as applied or skipped, with the reason.
        """
        def skip(lesson: dict, reason: str, level: str = "info") -> None:
            self.db.log(level, f"Lesson #{lesson['id']} ({lesson.get('param_name')}): "
                               f"{reason}, not applied")
            try:
                self.db.record_lesson_skip(lesson["id"], reason)
            except Exception:  # noqa: BLE001 - provenance must never stop the apply loop
                log.debug("record_lesson_skip failed", exc_info=True)

        try:
            active_lessons = self.db.active_lessons()
            applied = 0
            # Settings is a frozen dataclass: collect the changes and swap in a new instance at
            # the end (a direct setattr raises FrozenInstanceError and nothing would apply).
            changes: dict = {}
            for lesson in active_lessons:
                param_name = lesson.get("param_name")
                if param_name in NEVER_AUTO_APPLY:
                    skip(lesson, f"{param_name} is a risk limit and is never auto-applied")
                    continue
                if param_name not in AUTO_APPLICABLE:
                    skip(lesson, f"{param_name} is not an auto-applicable parameter")
                    continue

                # Check statistical validation
                sample_size = lesson.get("sample_size") or 0
                min_sample = lesson.get("min_sample_size") or 30
                p_value = lesson.get("p_value")

                if sample_size < min_sample:
                    skip(lesson, f"sample {sample_size} below minimum {min_sample}")
                    continue
                if p_value is None:
                    skip(lesson, "no p-value recorded")
                    continue
                if p_value >= 0.05:
                    skip(lesson, f"p-value {p_value} is not below 0.05")
                    continue

                # Validate proposed value is within bounds
                param_value = lesson.get("param_value")
                if param_value is None:
                    skip(lesson, "no proposed value")
                    continue
                min_val, max_val = AUTO_APPLICABLE[param_name]
                if not (min_val <= param_value <= max_val):
                    skip(lesson, f"value {param_value} outside bounds [{min_val}, {max_val}]",
                         "warn")
                    continue

                # Apply the lesson
                old_value = changes.get(param_name, getattr(self.s, param_name, None))
                if old_value is None:
                    skip(lesson, f"current value of {param_name} is unknown")
                    continue
                if isinstance(old_value, int) and not isinstance(old_value, bool):
                    param_value = int(round(param_value))
                if abs(old_value - param_value) > 1e-6:
                    changes[param_name] = param_value
                    self.db.log("info", f"Applied lesson #{lesson['id']}: {param_name} "
                                        f"{old_value} -> {param_value} (sample={sample_size}, p={p_value})")
                    applied += 1
                    reason = f"{old_value} -> {param_value}"
                else:
                    reason = f"already in effect ({param_value})"
                try:
                    self.db.record_lesson_applied(lesson["id"], reason)
                except Exception:  # noqa: BLE001
                    log.debug("record_lesson_applied failed", exc_info=True)

            if changes:
                self.s = dataclasses.replace(self.s, **changes)
                self.db.log("info", f"Applied {applied} active lesson(s) to engine settings")
                # Recreate the risk managers with the updated settings
                self.risk = RiskManager(self.s)
                self.option_risk = OptionRiskManager(self.s)

        except Exception as e:
            self.db.log("error", f"Failed to apply active lessons: {e}")

    def refresh_lessons(self) -> int:
        """Re-apply active lessons (call after review completes). Returns count applied."""
        old_sl = self.s.sl_atr_mult
        old_tp = self.s.tp_atr_mult
        old_ta = self.s.consensus_ta_min
        old_ml = self.s.ml_min_p

        self._apply_active_lessons()

        # Count how many actually changed
        changes = 0
        if abs(self.s.sl_atr_mult - old_sl) > 1e-6: changes += 1
        if abs(self.s.tp_atr_mult - old_tp) > 1e-6: changes += 1
        if abs(self.s.consensus_ta_min - old_ta) > 1e-6: changes += 1
        if abs(self.s.ml_min_p - old_ml) > 1e-6: changes += 1
        return changes

    def effective_run_params(self) -> dict:
        """The whitelisted tunable parameters plus the key gates, as they are right now."""
        out: dict = {}
        for name in (*AUTO_APPLICABLE, *RUN_PARAM_GATES):
            if hasattr(self.s, name):
                val = getattr(self.s, name)
                out[name] = val if isinstance(val, (int, float, str, bool, type(None))) else str(val)
        out["trading_mode"] = self.trading_mode
        return out

    def save_run_params(self, run_id: int) -> None:
        """M9: persist the effective parameters this run used (provenance)."""
        try:
            self.db.save_run_params(run_id, self.effective_run_params())
        except Exception:  # noqa: BLE001 - provenance must never stop a run
            log.debug("save_run_params failed", exc_info=True)

    # -- state -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._run_lock.locked() or self._cycle_active

    @property
    def scheduler_on(self) -> bool:
        return self._scheduler is not None and not self._scheduler.done()

    @property
    def paused(self) -> bool:
        """"Pause engine": the scheduler is off, nothing was dismissed, exits keep running."""
        return not self.scheduler_on

    def set_mode(self, mode: str) -> None:
        if mode not in ("approve", "auto"):
            raise ValueError("mode must be 'approve' or 'auto'")
        if mode == "auto" and self.s.is_live and not self.s.allow_live_auto:
            raise ValueError(
                "Auto mode on a live account needs ALLOW_LIVE_AUTO_TRADING=true in .env"
            )
        self.trading_mode = mode
        self.db.log("info", f"Trading mode set to {mode}")

    async def set_kill_switch(self, on: bool, reason: str = "turned on by you") -> None:
        if on and self.kill_switch:
            return
        self.kill_switch = on
        self.kill_reason = reason if on else None
        self.kill_at = now_utc().isoformat(timespec="seconds") if on else None
        self.db.set_kv("kill_switch", {"on": on, "reason": self.kill_reason, "at": self.kill_at})
        # Publish before the slow part (stopping the scheduler) so the UI flips immediately.
        hub.publish("kill", {"on": on, "reason": self.kill_reason,
                             "at": self.kill_at or now_utc().isoformat(timespec="seconds")})
        if on:
            await self.stop_scheduler()
            n = self.db.dismiss_all_pending("kill switch")
            self.db.log("error", f"KILL SWITCH ON ({reason}): scheduler stopped, {n} pending "
                                 "proposal(s) dismissed, new orders blocked. Orders already at "
                                 "Webull, including protective stops, stay in place unless the "
                                 "kill action cancels them. Exits (option stops/targets/expiry "
                                 "closes, flatten) keep working.", alert=True)
        else:
            self.run_failures = self.order_failures = 0
            self.last_failure = None
            self.db.log("info", "Kill switch released", alert=True)

    def _failed(self, kind: str, message: str) -> None:
        if kind == "run":
            self.run_failures += 1
        else:
            self.order_failures += 1
        self.last_failure = message

    # In-memory counter for proposal attempts per run (includes early rejections)
    # This ensures the per-run cap counts ALL attempts, not just ones that reach the DB.
    _proposal_attempts: dict[int, int]
