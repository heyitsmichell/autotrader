"""Core-satellite rebalancing."""

from __future__ import annotations

from ..broker.market_data import fresh_positions
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
    from ._host import RebalanceHost as _Host
else:
    _Host = object

class RebalanceMixin(_Host):
    async def check_core_rebalance(self) -> dict | None:
        """Check if core rebalancing is needed and execute if so.

        This implements the core-satellite portfolio (Phase 4):
        - Core holds CORE_SYMBOL (default SPY) at CORE_TARGET_PCT of equity
        - Satellite is the active strategy, bounded by SATELLITE_MAX_PCT
        - Rebalancing happens when core drifts beyond CORE_REBALANCE_BAND_PCT
        - Orders go through the normal risk checks (RiskManager.check)
        - Drawdown guard can shrink/pause/flatten satellite when it exceeds SATELLITE_DRAWDOWN_GUARD_PCT

        Returns a dict with the result, or None if no action taken.
        """
        if not self.s.core_enabled:
            return None
        if self.kill_switch:
            return None
        # Mechanical rebalancing is automatic trading: it follows the same rules as auto mode.
        if self.trading_mode != "auto":
            return None
        if self.s.is_live and not self.s.allow_live_auto:
            return None

        # Get current portfolio state from broker
        try:
            bal = await self.broker.account_balance()
            positions = await self.broker.positions()
        except BrokerError as e:
            self.db.log("warn", f"Core rebalance check failed: could not read account: {e}")
            return None

        total_equity = bal.get("net_liq", 0.0)
        if total_equity <= 0:
            return None

        core_symbol = self.s.core_symbol.upper()
        core_qty = int(positions.get(core_symbol, 0))
        core_price = bal.get(f"{core_symbol}_last", 0.0)
        if core_price <= 0:
            # Try to get live price
            try:
                core_price = await self.broker.last_price(core_symbol)
            except BrokerError:
                core_price = 0.0
        if core_price <= 0:
            self.db.log("warn", f"Core rebalance: no price for {core_symbol}")
            return None

        # Calculate satellite positions value (everything except core)
        satellite_value = 0.0
        for sym, qty in positions.items():
            if sym.upper() == core_symbol:
                continue
            try:
                last = await self.broker.last_price(sym)
                if last and last > 0:
                    satellite_value += qty * last
            except BrokerError:
                pass

        # Calculate core state
        state = calculate_core_state(
            total_equity=total_equity,
            core_position_qty=core_qty,
            core_price=core_price,
            satellite_positions_value=satellite_value,
            settings=self.s,
        )

        # Check satellite drawdown guard
        satellite_peak = self.db.get_kv("satellite_peak_equity")
        if satellite_peak is None:
            satellite_peak = satellite_value
            self.db.set_kv("satellite_peak_equity", satellite_peak)
        elif satellite_value > satellite_peak:
            satellite_peak = satellite_value
            self.db.set_kv("satellite_peak_equity", satellite_peak)

        drawdown_guard = check_satellite_drawdown_guard(
            satellite_peak_equity=satellite_peak,
            satellite_current_equity=satellite_value,
            settings=self.s,
        )

        # If drawdown guard is triggered (pause or flatten_satellite), log it
        if drawdown_guard.action in ("pause", "flatten_satellite"):
            self.db.log("warn", f"Satellite drawdown guard: {drawdown_guard.reason}", alert=True)
            if drawdown_guard.action == "flatten_satellite":
                # Flatten all satellite positions
                return await self._flatten_satellite(core_symbol, "satellite drawdown guard")

        # Create rebalance plan
        is_market_hours = market_is_open()
        plan = create_rebalance_plan(state, core_price, self.s, is_market_hours)

        report = format_core_satellite_report(state, drawdown_guard, plan)
        self.db.log("info", f"Core-satellite status: core {state.core_actual_pct:.1f}% (target {state.core_target_pct:.1f}%), "
                         f"satellite {state.satellite_actual_pct:.1f}% (max {state.satellite_max_pct:.1f}%), "
                         f"drift {state.drift_pct:+.1f}%, action: {plan.action}")

        # Execute rebalance if needed
        if plan.action in ("buy_core", "sell_core"):
            if not is_market_hours and self.s.core_rebalance_market_hours_only:
                return {"action": "deferred", "reason": plan.reason, "report": report}

            # Place the order through the normal execution path
            side = "BUY" if plan.action == "buy_core" else "SELL"
            p = {
                "symbol": core_symbol,
                "side": side,
                "quantity": plan.shares,
                "limit_price": round(core_price, 2),
                "confidence": 1.0,
                "rationale": f"Core rebalance: {plan.reason}",
            }

            # Bypass consensus gate for core rebalancing (it's a mechanical rebalance)
            # But still run risk checks
            verdict = await self.risk.check(p, self.broker, self.db,
                                            kill_switch=self.kill_switch, at_execution=True,
                                            watchlist=self.research.effective_watchlist() if self.research else self.s.watchlist)

            if not verdict.ok:
                reason = "; ".join(verdict.reasons)
                self.db.log("warn", f"Core rebalance {side} {plan.shares} {core_symbol} blocked by risk: {reason}")
                return {"action": "blocked", "reason": reason, "report": report}

            # Create proposal and execute. A DuplicateProposalError (an open row for this
            # symbol and side already exists) skips a rebalance BUY; a SELL reduces risk, so it
            # settles the open row out of the way instead (see _add_exit_row).
            extra = dict(signal_id=None, sl=None, tp=None, ml_p=None, consensus=[],
                         risk_checks=verdict.checks)
            try:
                if side == "SELL":
                    pid = self._add_exit_row(p, "pending", "", core_price, **extra)
                    if pid is None:
                        return {"action": "failed", "reason": "could not record the order",
                                "report": report}
                else:
                    pid = self.db.add_proposal(None, p, "pending", "", core_price, None, **extra)
            except DuplicateProposalError:
                self.db.log("info", f"Core rebalance {side} {core_symbol} skipped: an open "
                                    "proposal already exists")
                return {"action": "skipped", "reason": "open proposal exists", "report": report}

            # Claim and execute
            if not self.db.claim_pending(pid):
                return {"action": "failed", "reason": "could not claim proposal", "report": report}

            try:
                await self._send(pid, p, "core_rebalance", f"#{pid} {side} {plan.shares} {core_symbol} @ {core_price:.2f}")
            except Exception as e:
                self.db.log("error", f"Core rebalance execution failed: {e}")
                return {"action": "failed", "reason": str(e), "report": report}

            return {"action": plan.action, "proposal_id": pid, "shares": plan.shares, "report": report}

        return {"action": "none", "report": report}

    async def _satellite_headroom_usd(self) -> float:
        """USD still available for new satellite buys (BrokerError propagates: callers fail closed)."""
        bal = await self.broker.account_balance()
        positions = await fresh_positions(self.broker)
        core = self.s.core_symbol.upper()
        sat_value = 0.0
        for sym, qty in positions.items():
            if sym.upper() == core or qty <= 0:
                continue
            last = await self.broker.last_price(sym)
            if not last or last <= 0:
                raise BrokerError(f"no price for {sym}; cannot value the satellite")
            sat_value += qty * last
        peak = self.db.get_kv("satellite_peak_equity")
        peak = float(peak) if peak is not None else sat_value
        guard = check_satellite_drawdown_guard(peak, sat_value, self.s)
        return satellite_headroom_usd(float(bal.get("net_liq", 0.0)), sat_value, guard.new_satellite_max_pct)

    async def _flatten_satellite(self, core_symbol: str, reason: str) -> dict:
        """Flatten all satellite positions (everything except core)."""
        try:
            positions = await fresh_positions(self.broker)
        except BrokerError as e:
            return {"action": "failed", "reason": f"could not read positions: {e}"}

        results = []
        for sym, qty in positions.items():
            if sym.upper() == core_symbol:
                continue
            if qty <= 0:
                continue
            try:
                last = await self.broker.last_price(sym)
            except BrokerError:
                last = None
            if not last:
                results.append({"symbol": sym, "status": "no_price"})
                continue

            limit = round(last * (1 - self.s.flatten_slippage_pct / 100), 2)
            p = {"symbol": sym, "side": "SELL", "quantity": int(qty), "limit_price": limit,
                 "confidence": 1.0, "rationale": f"Flatten satellite: {reason}"}

            # Risk check (should pass for SELL)
            verdict = await self.risk.check(p, self.broker, self.db,
                                            kill_switch=self.kill_switch, at_execution=True,
                                            watchlist=self.research.effective_watchlist() if self.research else self.s.watchlist)
            if not verdict.ok:
                results.append({"symbol": sym, "status": "blocked", "reason": verdict.reasons})
                continue

            pid = self._add_exit_row(p, "pending", "", limit, signal_id=None, sl=None, tp=None,
                                     ml_p=None, consensus=[], risk_checks=verdict.checks)
            if pid is None or not self.db.claim_pending(pid):
                results.append({"symbol": sym, "status": "claim_failed"})
                continue

            try:
                async with self.broker.flatten_mode():
                    await self._send(pid, p, "core_rebalance",
                                     f"#{pid} SELL {int(qty)} {sym} @ {limit:.2f}")
                results.append({"symbol": sym, "status": "placed", "proposal_id": pid})
            except Exception as e:
                results.append({"symbol": sym, "status": "failed", "reason": str(e)})

        return {"action": "flatten_satellite", "results": results}
