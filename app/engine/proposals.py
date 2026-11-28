"""Proposal generation and validation."""

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
from ..risk import OptionRiskManager, RiskManager, working_buy_symbols
from ..sizing import max_allowed_qty, ml_size_factor, volatility_scaled_qty
from .util import (_coid, _is_option_symbol, _session_rejection,
                   normalize_proposal)

log = logging.getLogger("app.engine")


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing-only host contract; no runtime base class
    from ._host import ProposalsHost as _Host
else:
    _Host = object

class ProposalsMixin(_Host):
    async def _generate_proposals_deterministic(self, run_id: int, sigs: list[dict]) -> int:
        """Generate trade proposals deterministically (no LLM).

        BUY logic: For each symbol where consensus gate passes ("long"),
        submit a BUY proposal at the current price with code-sized quantity.

        SELL logic: For each held position, app/exits.py decides whether to exit:
        - Time stop (EXIT_MAX_HOLD_DAYS)
        - Trend "down" on EXIT_CONFIRM_READINGS consecutive readings
        - ML probability below ML_MIN_P by more than EXIT_ML_MARGIN (if model active)
        - Weakness below the exit bar (EXIT_TA_FLOOR) while the trend is not up
        """
        proposals_generated = 0
        watchlist = self.research.effective_watchlist() if self.research else self.s.watchlist

        # Get current positions
        try:
            positions = await self.broker.positions()
        except BrokerError as e:
            self.db.log("warn", f"Could not read positions for deterministic proposals: {e}")
            positions = None     # unknown (not "flat"): no profit-state reset, no SELL section

        # --- BUY proposals: symbols where consensus gate passes ---
        # ML-first: best model score first, and at most ML_FIRST_TOP_N new BUYs per cycle.
        # Without any model score (no promoted model) the order and cap are unchanged.
        ml_ranked = bool(self.s.ml_enabled and self.s.ml_first
                         and any(x.get("ml_p") is not None for x in sigs))
        if ml_ranked:
            sigs = sorted(sigs, key=lambda x: (x.get("ml_p") is None, -(x.get("ml_p") or 0.0)))
        buys_generated = 0
        working_syms = working_buy_symbols(self.db)   # followup-correctness: no re-propose
        for sig in sigs:
            if ml_ranked and buys_generated >= self.s.ml_first_top_n:
                break

            symbol = sig["symbol"]
            if symbol not in watchlist:
                continue

            # Only propose BUY if consensus gate currently passes
            if sig.get("consensus") != "long":
                continue

            if symbol in working_syms:
                continue   # entry still working at the broker; would only be rejected

            # Check if we already have a pending proposal for this symbol
            pending = self.db.pending_proposals()
            if any(p["symbol"] == symbol and p["side"] == "BUY" for p in pending):
                continue

            # Check if we already hold this symbol
            held = (positions or {}).get(symbol, 0.0)
            if held > 0:
                continue  # Already have a position, don't double up

            # Get live price for the limit order
            try:
                last_price = await self.broker.last_price(symbol)
            except BrokerError as e:
                self.db.log("warn", f"Could not get price for {symbol}: {e}")
                continue
            if not last_price or last_price <= 0:
                continue
            # Outside CORE the price must be the extended-hours/overnight print. Only the stale
            # regular close (no extended print yet) -> do not anchor an entry to it.
            try:
                stale = await self.broker.is_price_stale(symbol)
            except Exception as exc:  # noqa: BLE001 - stub brokers without the method
                log.debug("is_price_stale check failed (assuming fresh): %s", exc)
                stale = False
            if stale:
                self.db.log("info", f"Skipping BUY {symbol}: no extended-hours price; the "
                                    f"regular close ${last_price:.2f} is stale")
                continue

            # Build proposal arguments (code will validate and size in _on_proposal)
            p = {
                "symbol": symbol,
                "side": "BUY",
                "quantity": 1,  # Will be clamped by _on_proposal to max_allowed_qty
                "limit_price": round(float(last_price), 2),
                # Model probability when a model scored this symbol, else the old fixed value.
                "confidence": round(float(sig["ml_p"]), 2) if sig.get("ml_p") is not None else 0.7,
                "rationale": f"Consensus gate passed: TA score {sig.get('ta_score') or 0:+.2f}, "
                           f"news {sig.get('sent_score') or 0:+.2f}, "
                           f"ML {sig.get('ml_p')}"
                           + (f" (ML-first pick #{buys_generated + 1})" if ml_ranked else ""),
            }

            # Submit through _on_proposal (does all validation, gating, risk checks)
            result = await self._on_proposal(run_id, p)
            if result.startswith("ACCEPTED"):
                proposals_generated += 1
                buys_generated += 1
                self.db.log("info", f"Deterministic BUY generated for {symbol}: {result}")

        # --- Option entries (long calls alongside the stock); off unless OPTIONS_AUTO_ENTRY ---
        if self.s.options_enabled and self.s.options_auto_entry:
            try:
                proposals_generated += await self.propose_option_entries(run_id, sigs)
            except Exception as e:  # noqa: BLE001 - option entries must never stop the exits below
                log.exception("option entries failed")
                self.db.log("warn", f"Option entries failed: {type(e).__name__}: {e}")

        # --- SELL proposals: check held positions for exit signals ---
        # Rules live in app/exits.py (time stop, confirmed trend break, ML drop, and a TA exit
        # bar that sits below the entry bar so positions are not churned by small wobbles).
        entry_times: dict[str, str] = {}
        entry_prices: dict[str, float] = {}
        try:
            for tr in reversed(self.db.open_trades()):          # oldest first, so the NEWEST wins
                if not tr.get("entry_fill_price"):
                    continue          # unfilled rows must not reset the time stop / profit entry
                entry_times[tr["symbol"]] = tr.get("entry_fill_time") or tr.get("created_at")
                entry_prices[tr["symbol"]] = float(tr["entry_fill_price"])
        except Exception as e:  # noqa: BLE001 - ledger problems must not stop exits
            self.db.log("warn", f"Could not read open trades for time stops: {e}")

        if positions is None:
            return proposals_generated      # positions unreadable: skip the reset and the SELLs

        # Forget profit-taking state for positions that no longer exist, so a re-entry starts clean.
        for key in list(self.db.get_kv("profit_state_symbols", [])):
            if positions.get(key, 0.0) <= 0:
                self.db.set_kv(f"hwm:{key}", None)
                self.db.set_kv(f"scaled:{key}", 0)
        state_symbols = [k for k, q in positions.items() if q > 0]
        self.db.set_kv("profit_state_symbols", state_symbols)

        for symbol, qty in positions.items():
            if qty <= 0:
                continue

            # Get signal for this symbol
            try:
                sig = await self.signals.ensure(symbol, max_age_minutes=60, allow_stale=True)
            except Exception as e:
                self.db.log("warn", f"Could not get signal for {symbol}: {e}")
                continue
            if not sig:
                continue

            history = self.db.signals(symbol, max(1, self.s.exit_confirm_readings))
            # The core holding is managed by the rebalancer, so it gets no time stop.
            is_core = self.s.core_enabled and symbol.upper() == self.s.core_symbol.upper()
            days = None if is_core else exits.held_days(entry_times.get(symbol), now_utc())
            # Live profit-taking inputs: entry price from the ledger, current price from the broker,
            # and a persisted high-water mark (the old call never passed these, so the trailing
            # stop in exits.py could never fire live).
            entry_px = entry_prices.get(symbol)
            cur_px = None
            peak_px = None
            # The high-water mark and scale-out count belong to ONE entry. A re-entry within a
            # cycle (the flat-position reset above never saw it) must not inherit the previous
            # trade's peak: that made a fresh BUY "give back 100%+" and sell at once (churn).
            entry_key = str(entry_times.get(symbol) or "")
            if entry_px and not is_core and self.db.get_kv(f"profit_entry:{symbol}", None) != entry_key:
                self.db.set_kv(f"hwm:{symbol}", None)
                self.db.set_kv(f"scaled:{symbol}", 0)
                self.db.set_kv(f"profit_entry:{symbol}", entry_key)
            if entry_px and not is_core:
                try:
                    cur_px = await self.broker.last_price(symbol)
                except BrokerError:
                    cur_px = None
                if cur_px:
                    prev_peak = self.db.get_kv(f"hwm:{symbol}", None)
                    peak_px = max(float(prev_peak or entry_px), float(cur_px))
                    if prev_peak is None or peak_px > float(prev_peak):
                        self.db.set_kv(f"hwm:{symbol}", peak_px)
            tranches = int(self.db.get_kv(f"scaled:{symbol}", 0) or 0)
            decision = exits.decide_exit(sig, [decode(r) for r in history], self.s,
                                         held_for_days=days, entry_price=entry_px,
                                         current_price=cur_px, highest_close_since_entry=peak_px,
                                         tranches_done=tranches)
            if decision is None:
                continue
            if decision.sell_fraction < 1.0:
                sell_qty = exits.partial_quantity(qty, decision.sell_fraction, self.s, symbol)
            else:
                sell_qty = exits.sell_quantity(qty, self.s, symbol)
            if sell_qty <= 0:
                continue
            ok = await self._propose_sell(symbol, sell_qty, run_id, decision.reason, proposals_generated)
            if ok:
                proposals_generated += 1
                if decision.tranche:
                    self.db.set_kv(f"scaled:{symbol}", decision.tranche)

        return proposals_generated

    async def _propose_sell(self, symbol: str, qty: float, run_id: int, rationale: str, proposals_generated: int) -> bool:
        """Helper to submit a deterministic SELL proposal. True only when the proposal was accepted."""
        # Check if we already have a pending SELL for this symbol
        pending = self.db.pending_proposals()
        if any(p["symbol"] == symbol and p["side"] == "SELL" for p in pending):
            return False

        # Get live price
        try:
            last_price = await self.broker.last_price(symbol)
        except BrokerError:
            return False
        if not last_price or last_price <= 0:
            return False

        p = {
            "symbol": symbol,
            "side": "SELL",
            "quantity": qty,
            "limit_price": round(float(last_price) * 0.99, 2),  # Slightly below market for quick fill
            "confidence": 0.8,
            "rationale": f"Deterministic exit: {rationale}",
        }

        result = await self._on_proposal(run_id, p)
        if result.startswith("ACCEPTED"):
            self.db.log("info", f"Deterministic SELL generated for {symbol}: {result}")
            return True
        self.db.log("warn", f"Deterministic SELL for {symbol} not accepted: {result}")
        return False

    def _max_allowed_qty(self, p: dict, last_price: float, held: float) -> float:
        """Compute the maximum allowed quantity for a BUY based on all risk limits."""
        return max_allowed_qty(self.s, limit_price=p["limit_price"], last_price=last_price,
                               held=held, stop_loss=p.get("sl"))

    async def _on_proposal(self, run_id: int, args: dict, sig_override: dict | None = None) -> str:
        """Validate, gate and store one proposal. The reply is a short status string.

        Serialized behind ``_proposal_lock`` (QA M3): the slow cycle and the fast loop both come
        through here, so the "is there already a pending BUY?" read and the insert that follows
        it can never interleave. The DB's partial unique index is the second line of defence;
        a ``DuplicateProposalError`` from it is a skip, never a crash.
        """
        async with self._proposal_lock:
            try:
                return await self._on_proposal_locked(run_id, args, sig_override)
            except DuplicateProposalError as e:
                sym = str(args.get("symbol", "")).upper()
                side = str(args.get("side", "")).upper()
                self.db.log("info", f"Proposal for {side} {sym} skipped: an open proposal already "
                                    f"exists ({e})")
                return (f"REJECTED (duplicate): an open {side} proposal for {sym} already exists. "
                        "Wait for it to be decided.")

    async def _on_proposal_locked(self, run_id: int, args: dict,
                                  sig_override: dict | None = None) -> str:
        # Kill switch checked first - blocks OPENING orders immediately. Exits (equity SELL, option
        # *_TO_CLOSE: time stop, trend, ML exits) always pass (standing decision).
        if self.kill_switch and self._is_opening(
                {"symbol": str(args.get("symbol", "")).upper(), "side": args.get("side"),
                 "position_intent": args.get("position_intent")}):
            return f"REJECTED (kill switch): {self.kill_reason or 'kill switch is on'}"


        # Validate arguments at the tool boundary before creating a proposal row
        try:
            p = normalize_proposal(args)
        except (TypeError, ValueError) as e:
            return f"REJECTED (invalid arguments): {e}. Fix the arguments and resubmit."

        # Additional schema validation for clear error messages
        s = self.s
        qty = p.get("quantity")
        is_frac = isinstance(qty, float) and qty != int(qty)
        if is_frac and not s.fractional_enabled:
            return "REJECTED (invalid arguments): fractional quantity requires FRACTIONAL_ENABLED=true."
        if not isinstance(qty, (int, float)) or qty <= 0:
            return "REJECTED (invalid arguments): quantity must be positive."
        if is_frac and qty < 10 ** (-s.fractional_decimals):
            return f"REJECTED (invalid arguments): quantity {qty} below minimum precision (10^-{s.fractional_decimals})."
        if not isinstance(p.get("limit_price"), (int, float)) or p["limit_price"] <= 0:
            return "REJECTED (invalid arguments): limit_price must be a positive number."
        if p.get("side") not in ("BUY", "SELL"):
            return "REJECTED (invalid arguments): side must be BUY or SELL."

        # One open proposal per symbol and side (the DB index enforces it too).
        if any(x["symbol"] == p["symbol"] and x["side"] == p["side"]
               for x in self.db.pending_proposals()):
            return (f"REJECTED (duplicate): an open {p['side']} proposal for {p['symbol']} "
                    "already exists. Wait for it to be decided.")

        # Check if this is an option symbol (OCC format)
        is_option = _is_option_symbol(p["symbol"])

        if is_option:
            # Options path: validate option-specific fields
            if not s.options_enabled:
                return "REJECTED (options): options trading is disabled (OPTIONS_ENABLED=false)"

            # Required fields for options
            required_option_fields = ["option_type", "strike_price", "expiration_date", "position_intent"]
            for field in required_option_fields:
                if field not in p or not p[field]:
                    return f"REJECTED (options): missing required field '{field}' for option order"

            # Validate option_type
            if p["option_type"] not in ("CALL", "PUT"):
                return f"REJECTED (options): option_type must be CALL or PUT, got {p['option_type']}"

            # Validate position_intent
            valid_intents = {"BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE"}
            if p["position_intent"] not in valid_intents:
                return f"REJECTED (options): position_intent must be one of {valid_intents}, got {p['position_intent']}"

            # Validate expiration_date format (YYYY-MM-DD)
            import re
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", p["expiration_date"]):
                return f"REJECTED (options): expiration_date must be YYYY-MM-DD, got {p['expiration_date']}"

            # Validate strike_price
            if not isinstance(p["strike_price"], (int, float)) or p["strike_price"] <= 0:
                return f"REJECTED (options): strike_price must be positive"

            # Validate quantity is integer (contracts)
            if not isinstance(p["quantity"], int) or p["quantity"] <= 0:
                return f"REJECTED (options): quantity must be a positive integer (contracts)"

            # Run option risk checks
            ok, reasons = self.option_risk.check_single_leg(p)
            if not ok:
                return f"REJECTED (options risk): {'; '.join(reasons)}"
            # S14: opening option orders obey the account-level daily-loss / trade-count limits
            ok, reasons = await self.option_risk.check_option_open_account(p, self.broker, self.db)
            if not ok:
                return f"REJECTED (options risk): {'; '.join(reasons)}"

            # For options, run the option consensus gate (IV rank, delta, liquidity)
            sig = None
            last_price = p["limit_price"]  # For options, limit_price is the premium

            # Options don't use fractional or equity sizing
            p["_is_fractional"] = False  # Options are always whole contracts
            p["_use_brackets"] = False   # Options don't use bracket orders

            # Run option consensus gate
            gate = await self._option_consensus_gate(p)
            p["sl"], p["tp"] = gate["sl"], gate["tp"]

            if not gate["ok"]:
                failed = [c["detail"] for c in gate["checks"] if not c["ok"]]
                reason = "; ".join(failed)
                pid = self.db.add_proposal(
                    run_id, p, "rejected", reason, last_price,
                    None, signal_id=None, sl=None, tp=None,
                    ml_p=None, consensus=gate["checks"], risk_checks=[],
                    asset_class="option", option_type=p.get("option_type"),
                    strike_price=p.get("strike_price"),
                    expiration_date=p.get("expiration_date"),
                    position_intent=p.get("position_intent"),
                    instrument_json=json.dumps({
                        "option_type": p.get("option_type"),
                        "strike_price": p.get("strike_price"),
                        "expiration_date": p.get("expiration_date"),
                        "position_intent": p.get("position_intent"),
                        "multiplier": 100,
                    }),
                    strategy_type=p.get("option_strategy", "SINGLE"),
                    legs=json.dumps(p.get("legs")) if p.get("legs") else None,
                )
                self.db.log("warn", f"Option proposal #{pid} rejected by consensus gate: {reason}")
                return f"REJECTED option proposal #{pid} (consensus gate): {reason}. You may submit a corrected proposal."

            # Option consensus gate passed - create proposal
            return await self._create_option_proposal(pid=None, p=p, gate=gate, verdict=None, run_id=run_id)

        else:
            # Equity path (existing logic)
            watchlist = self.research.effective_watchlist() if self.research else self.s.watchlist
            if p["symbol"] not in watchlist:
                return f"REJECTED (watchlist): {p['symbol']} is not on the watchlist. Use get_signals to see valid symbols."

            # Fetch live price for validation and clamping
            try:
                last_price = await self.broker.last_price(p["symbol"])
            except BrokerError as e:
                return f"REJECTED (no price): could not fetch a price for {p['symbol']}: {e}"
            if last_price is None or last_price <= 0:
                return f"REJECTED (no price): no current price available for {p['symbol']}"

            # Check price band early
            deviation = abs(p["limit_price"] - last_price) / last_price * 100
            if deviation > self.s.price_band_pct:
                return (f"REJECTED (price band): limit ${p['limit_price']:.2f} is {deviation:.2f}% "
                        f"from last ${last_price:.2f} (max {self.s.price_band_pct}%).")

            # Get signal
            sig = sig_override or self.signal_store.get(p["symbol"], max_age=20 * 60)
            if sig is None:
                try:   # the warm store missed: one bounded refresh for this symbol
                    sig = await asyncio.wait_for(self.signals.ensure(p["symbol"], allow_stale=(p["side"] == "SELL")), 60)
                except asyncio.TimeoutError:
                    sig = None
            if not sig:
                return f"REJECTED (no signal): no signal available for {p['symbol']} yet."

            # Entry eligibility checks (Step 3) - run before consensus gate so they appear in the gate's check list
            elig = await check_eligibility(self.s, self.broker, self.signals, p["symbol"], p["side"], last_price, sig, self.db)
            if not elig.ok:
                return f"REJECTED (eligibility): {elig.reason}"

            # Check held shares for SELL
            if p["side"] == "SELL":
                try:
                    held = (await self.broker.positions()).get(p["symbol"], 0.0)
                except BrokerError as e:
                    return f"REJECTED (positions): could not read positions: {e}"
                if p["quantity"] > held:
                    return (f"REJECTED (no shorting): cannot sell {p['quantity']} {p['symbol']}: "
                            f"account holds {held:g} (no shorting).")

            gate = consensus.evaluate(p, sig, self.s)
            p["sl"], p["tp"] = gate["sl"], gate["tp"]

            # Add eligibility check results to the gate's check list so they appear in the same pass/fail display
            if elig.details:
                gate["checks"].append({
                    "name": "Eligibility",
                    "ok": elig.ok,
                    "detail": elig.reason or "All eligibility checks passed",
                    "value": elig.details,
                    "threshold": None
                })

        # Validate fractional quantity early (before sizing) so the original proposal quantity is checked
        qty = p["quantity"]
        is_frac_qty = isinstance(qty, float) and qty != int(qty)
        p["_is_fractional"] = is_frac_qty  # Pass to risk check
        if is_frac_qty and not self.s.fractional_enabled:
            return "REJECTED (fractional): fractional trading is disabled"
        if is_frac_qty and self.s.fractional_enabled:
            if p["symbol"].upper() not in self.s.fractional_whitelist:
                return f"REJECTED (fractional): {p['symbol']} is not on the fractional whitelist"
            if qty < 10 ** (-self.s.fractional_decimals):
                return (f"REJECTED (fractional): quantity {qty} below minimum precision "
                        f"(10^-{self.s.fractional_decimals})")
            rounded = round(qty, self.s.fractional_decimals)
            if rounded != qty:
                return (f"REJECTED (fractional): quantity {qty} exceeds "
                        f"{self.s.fractional_decimals} decimal places")

        # For BUY, clamp quantity to max allowed
        if p["side"] == "BUY":
            try:
                held = (await self.broker.positions()).get(p["symbol"], 0.0)
            except BrokerError as e:
                return f"REJECTED (positions): could not read positions: {e}"
            max_allowed = self._max_allowed_qty(p, last_price, held)
            if max_allowed == 0:
                return ("REJECTED (quantity): no capacity for this trade under current limits. "
                        f"max_order_qty={self.s.max_order_qty}, max_order_usd={self.s.max_order_usd}, "
                        f"max_position_usd={self.s.max_position_usd}, "
                        f"max_risk_per_trade_usd={self.s.max_risk_per_trade_usd}")
            # Core-satellite: new satellite buys may not exceed the satellite's share of equity.
            if self.s.core_enabled and p["symbol"].upper() != self.s.core_symbol.upper():
                try:
                    headroom = await self._satellite_headroom_usd()
                except BrokerError as e:
                    return f"REJECTED (satellite): could not size the satellite budget: {e}"
                cap = int(headroom // p["limit_price"]) if p["limit_price"] > 0 else 0
                if cap < 1:
                    return ("REJECTED (satellite): no satellite budget left "
                            f"(${headroom:,.0f} of the {self.s.satellite_max_pct:g}% satellite cap)")
                max_allowed = min(max_allowed, cap)
            # Code owns BUY sizing: the model picks symbol/price, the limits pick the size.
            # (SIZING_MODE=llm keeps the model's quantity and only clamps it down.)
            target = max_allowed if self.s.sizing_mode == "code" else min(p["quantity"], max_allowed)
            if self.s.sizing_mode == "code":
                # Model confidence scales the size down from the limit-based maximum (never up).
                factor = ml_size_factor(self.s, sig.get("ml_p"))
                if factor < 1.0:
                    target = max(1, round(target * factor))
                # Volatility scaling: adjust size so each position has similar ATR% risk
                atr = sig.get("atr")
                if atr and atr > 0:
                    target = volatility_scaled_qty(self.s, target, atr, p["limit_price"])
                # Re-apply hard limits after volatility scaling (vol scaling can increase qty up to max_factor)
                target = max_allowed_qty(self.s, limit_price=p["limit_price"], last_price=last_price,
                                          held=held, stop_loss=p.get("sl"), max_qty_override=target)
            # Bracket orders (OTOCO) require whole shares; round down if fractional trading is enabled
            use_brackets = self.s.bracket_orders
            if self.s.bracket_orders and self.s.fractional_enabled:
                # If target rounds down to 0, we can't use brackets - place as fractional order instead
                if int(target) >= 1:
                    target = float(int(target))
                    # After rounding down for brackets, the order is no longer fractional
                    p["_is_fractional"] = False
                else:
                    use_brackets = False
                    # We're not using brackets, so keep fractional flag for risk check message
                    # (don't clear it here - the original fractional flag is preserved)
            p["_use_brackets"] = use_brackets
            # After bracket rounding, check if we still have capacity
            # For fractional: minimum is 10^-decimals; for whole shares: minimum is 1
            min_qty = 10 ** (-self.s.fractional_decimals) if self.s.fractional_enabled else 1
            if target < min_qty:
                return ("REJECTED (quantity): no capacity for this trade under current limits. "
                        f"max_order_qty={self.s.max_order_qty}, max_order_usd={self.s.max_order_usd}, "
                        f"max_position_usd={self.s.max_position_usd}, "
                        f"max_risk_per_trade_usd={self.s.max_risk_per_trade_usd}")
            if target != p["quantity"]:
                p["quantity"] = target
                # Recompute gate with the code-sized quantity
                gate = consensus.evaluate(p, sig, self.s)
                p["sl"], p["tp"] = gate["sl"], gate["tp"]

        verdict = await self.risk.check(p, self.broker, self.db,
                                        kill_switch=self.kill_switch and self._is_opening(p),
                                        at_execution=False, watchlist=watchlist)
        failed = [c["detail"] for c in gate["checks"] if not c["ok"]] + verdict.reasons
        reason = "; ".join(failed)
        ok = gate["ok"] and verdict.ok
        status = "pending" if ok else "rejected"

        # Prepare extra fields for options
        extra = {
            "signal_id": sig["id"] if sig else None,
            "sl": p["sl"],
            "tp": p["tp"],
            "ml_p": sig.get("ml_p") if sig else None,
            "consensus": gate["checks"],
            "risk_checks": verdict.checks,
        }
        # Add option-specific fields if this is an option
        if _is_option_symbol(p["symbol"]):
            extra.update({
                "asset_class": "option",
                "option_type": p.get("option_type"),
                "strike_price": p.get("strike_price"),
                "expiration_date": p.get("expiration_date"),
                "position_intent": p.get("position_intent"),
                "instrument_json": json.dumps({
                    "option_type": p.get("option_type"),
                    "strike_price": p.get("strike_price"),
                    "expiration_date": p.get("expiration_date"),
                    "position_intent": p.get("position_intent"),
                    "multiplier": 100,
                }),
                "strategy_type": p.get("option_strategy", "SINGLE"),
                "legs": json.dumps(p.get("legs")) if p.get("legs") else None,
            })

        pid = self.db.add_proposal(
            run_id, p, status, reason, verdict.last_price,
            json.dumps(sig["features"]) if sig and sig.get("features") else None,
            **extra)
        what = f"#{pid} {p['side']} {p['quantity']:g} {p['symbol']} @ {p['limit_price']}"
        if not ok:
            stage = "consensus gate" if not gate["ok"] else "risk checks"
            self.db.log("warn", f"Proposal {what} rejected by {stage}: {reason}")
            return f"REJECTED proposal #{pid} ({stage}): {reason}. You may submit a corrected proposal."
        self.db.log("info", f"Proposal {what} passed the consensus gate and risk checks")
        # Alert when a proposal enters pending (for approve mode)
        use_brackets = p.get("_use_brackets", self.s.bracket_orders)
        if self.trading_mode == "approve" and hasattr(self, 'alerts') and self.alerts:
            bracket_msg = f"Bracket: stop {p['sl']}, target {p['tp']}." if use_brackets else "Protective orders will be placed separately after fill."
            self.alerts.notify(
                f"Pending proposal #{pid}: {p['side']} {p['quantity']} {p['symbol']} @ {p['limit_price']:.2f} "
                f"(expires in {self.s.proposal_ttl_minutes} min). "
                f"{bracket_msg}",
                level="warn",
                title="Trader: Proposal needs approval"
            )
        then = ("will be placed automatically when this run ends" if self.trading_mode == "auto"
                else "waits for the owner's approval")
        levels = (f" Bracket: stop {p['sl']}, target {p['tp']}."
                  if p["side"] == "BUY" and use_brackets else
                  (" Protective orders will be placed separately." if p["side"] == "BUY" else ""))
        # Add warning if symbol price exceeds MAX_ORDER_USD
        warning = ""
        if last_price > self.s.max_order_usd:
            warning = (f" WARNING: {p['symbol']} price ${last_price:.2f} exceeds MAX_ORDER_USD "
                       f"(${self.s.max_order_usd:,.0f}); 1-share granularity means it cannot be bought.")
        return f"ACCEPTED proposal #{pid}: passed all checks and {then}.{levels}{warning}"
    def dismiss(self, pid: int) -> None:
        p = self.db.get_proposal(pid)
        if p is None or p["status"] != "pending":
            raise ValueError(f"proposal #{pid} is not pending")
        self.db.update_proposal(pid, status="dismissed", reason="dismissed by you",
                                decided_by="user")
        self.db.log("info", f"Proposal #{pid} dismissed")
