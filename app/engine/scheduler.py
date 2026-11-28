"""Scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

from .. import consensus, exits, ta
from ..broker import Broker, BrokerError
from ..config import Settings, estimate_cost
from ..core import (calculate_core_state, create_rebalance_plan, check_satellite_drawdown_guard,
                    format_core_satellite_report, satellite_headroom_usd)
from ..db import DB, DuplicateProposalError, decode
from ..eligibility import check_eligibility
from ..eventhub import hub
from ..market import (market_is_open, market_is_open_extended, now_et, now_utc, order_session,
                      session_phase)
from ..portfolio import ACTIVE, DONE_CANCELLED, parse_orders
from ..risk import OptionRiskManager, RiskManager
from ..sizing import max_allowed_qty, ml_size_factor, volatility_scaled_qty
from .util import (_coid, _is_option_symbol, _session_rejection,
                   normalize_proposal)

log = logging.getLogger("app.engine")


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing-only host contract; no runtime base class
    from ._host import SchedulerHost as _Host
else:
    _Host = object

class SchedulerMixin(_Host):
    # -- scheduler -----------------------------------------------------------
    @property
    def cycle_minutes_effective(self) -> int:
        """The LLM never runs more often than LLM_MIN_CYCLE_MINUTES, so don't wake up faster."""
        return max(self.s.cycle_minutes, self.s.llm_min_cycle_minutes)

    def start_scheduler(self) -> None:
        if self.kill_switch:
            raise ValueError("Release the kill switch first")
        self.scheduler_stopped_by_user = False
        if not self.scheduler_on:
            self._scheduler = asyncio.create_task(self._scheduler_loop(), name="scheduler")
            note = ""
            if self.cycle_minutes_effective != self.s.cycle_minutes:
                note = (f" (CYCLE_MINUTES={self.s.cycle_minutes} is below LLM_MIN_CYCLE_MINUTES="
                        f"{self.s.llm_min_cycle_minutes}, so the LLM cycle is raised to match)")
            self.db.log("info", f"Scheduler started: every {self.cycle_minutes_effective} min"
                                + (" during market hours" if self.s.market_hours_only else "") + note)

    async def stop_scheduler(self) -> None:
        if self.scheduler_on:
            self._scheduler.cancel()
            try:
                await self._scheduler
            except asyncio.CancelledError:
                pass
            self.db.log("info", "Scheduler stopped")
        self._scheduler = None
        self.next_run_at = None

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                # MARKET_HOURS_ONLY=true limits runs to the regular session. Otherwise run in
                # premarket/regular/after-hours, and in the overnight session only when it
                # is enabled (that session is limited to the OVERNIGHT_SYMBOLS list).
                if self.s.market_hours_only:
                    market_open = market_is_open()
                elif self.s.overnight_trading:
                    market_open = order_session() is not None
                else:
                    market_open = market_is_open_extended()
                if market_open:
                    # Shielded: stopping the scheduler (or the kill switch) ends the loop but
                    # lets a run that already started finish cleanly instead of cutting it off
                    # mid-order. Anything it proposes after a kill is rejected by the checks.
                    self._current_run = asyncio.create_task(self.run_cycle("scheduler"))
                    await asyncio.shield(self._current_run)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("scheduled run failed")
            self.next_run_at = now_utc() + timedelta(minutes=self.cycle_minutes_effective)
            await asyncio.sleep(self.cycle_minutes_effective * 60)
