"""Order execution: execute, requote, _send."""

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
from ..order_style import (cancel_reply_confirmed, exit_should_be_market, marketable_buy_price,
                           market_rejection_is_definitive)
from ..risk import reprice_violation
from ..market import (market_is_open, market_is_open_extended, now_et, now_utc, order_session,
                      session_phase)
from ..portfolio import ACTIVE, DONE_CANCELLED, parse_orders
from ..portfolio_parsing import _age as _row_age_seconds
from ..risk import OptionRiskManager, RiskManager
from ..sizing import max_allowed_qty, ml_size_factor, overnight_scaled_qty, volatility_scaled_qty
from .util import (_coid, _is_option_symbol, _session_rejection,
                   classify_order_error, normalize_proposal)

log = logging.getLogger("app.engine")


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing-only host contract; no runtime base class
    from ._host import ExecutionHost as _Host
else:
    _Host = object

class ExecutionMixin(_Host):
    @staticmethod
    def _is_opening(p: dict) -> bool:
        """True for an order that opens or adds risk (the only kind the kill switch blocks).

        Equity: a BUY. Options: anything that is not ``*_TO_CLOSE``. Exits (equity SELL, option
        SELL_TO_CLOSE/BUY_TO_CLOSE) are never blocked by the kill switch (contracts section 0).
        """
        if _is_option_symbol(p.get("symbol") or ""):
            intent = str(p.get("position_intent") or "").upper()
            if intent:
                return not intent.endswith("_TO_CLOSE")
        return str(p.get("side", "BUY")).upper() == "BUY"

    async def _auto_execute_run(self, run_id: int) -> None:
        """TRADING_MODE=auto: place the run's pending proposals (slow cycle and fast loop).
        execute() re-checks the kill switch, live-auto rule and every risk limit."""
        if self.trading_mode != "auto":
            return
        for p in self.db.pending_for_run(run_id):
            try:
                await self.execute(p["id"], actor="auto")
            except ValueError as e:
                self.db.log("warn", f"Auto execution of #{p['id']} skipped: {e}")
            except Exception as e:  # noqa: BLE001 - one bad proposal must not skip the rest of the run
                log.exception("auto execution of #%s failed unexpectedly", p["id"])
                self.db.log("error", f"Auto execution of #{p['id']} failed unexpectedly "
                                     f"({type(e).__name__}): {e}")

    async def execute(self, pid: int, actor: str) -> dict:
        """Re-check risk, preview, then place the order. actor: 'user' or 'auto'."""
        async with self._exec_lock:
            p = self.db.get_proposal(pid)
            if p is None:
                raise ValueError(f"proposal #{pid} not found")
            if p["status"] != "pending":
                raise ValueError(f"proposal #{pid} is {p['status']}, not pending")
            # M1: the kill switch is re-checked here for EVERY asset type before anything
            # touches the broker. It blocks opening orders only; exits always go through.
            opening = self._is_opening(p)
            if self.kill_switch and opening:
                reason = f"kill switch is on: {self.kill_reason or 'no new orders'}"
                self.db.update_proposal(pid, status="rejected", reason=reason, decided_by=actor)
                self.db.log("warn", f"Order #{pid} {p['side']} {p['quantity']} {p['symbol']} "
                                    f"blocked at execution: {reason}")
                return self.db.get_proposal(pid)
            auto_candidates = set(self.db.get_kv("research_auto_watchlist", []))
            if (actor == "auto" and p["symbol"] in auto_candidates
                    and not self.s.auto_trade_research_candidates):
                raise ValueError(f"{p['symbol']} is an auto-discovered watchlist candidate; "
                                 "automatic execution of research candidates is disabled")
            if actor == "auto" and self.s.is_live and not self.s.allow_live_auto:
                raise ValueError("auto execution on a live account is disabled")
            what = f"#{pid} {p['side']} {p['quantity']} {p['symbol']} @ {p['limit_price']}"

            if opening and self.order_send_blocked():
                reason = ("the broker rejected the last order for its trading session; new orders "
                          "are paused until the session changes")
                self.db.update_proposal(pid, status="rejected", reason=reason, decided_by=actor)
                self.db.log("warn", f"Order {what} not sent: {reason}")
                return self.db.get_proposal(pid)

            # Check if this is an option order
            is_option = _is_option_symbol(p["symbol"])

            if is_option:
                # Options: re-run option risk checks at execution time
                ok, reasons = self.option_risk.check_single_leg(p)
                if ok and opening and hasattr(self.option_risk, "check_option_open_account"):
                    # S14: opening option orders also obey the daily-loss and trade-count limits
                    ok, reasons = await self.option_risk.check_option_open_account(
                        p, self.broker, self.db)
                if not ok:
                    reason = "; ".join(reasons)
                    self.db.update_proposal(pid, status="rejected", reason=reason, decided_by=actor)
                    self.db.log("warn", f"Option order {what} blocked at execution: {reason}")
                    return self.db.get_proposal(pid)
            else:
                # Equity: use existing risk manager
                watchlist = self.research.effective_watchlist() if self.research else self.s.watchlist
                verdict = await self.risk.check(p, self.broker, self.db,
                                                kill_switch=self.kill_switch and opening,
                                                at_execution=True, watchlist=watchlist)
                self.db.update_proposal(pid, risk_checks=json.dumps(verdict.checks))
                if not verdict.ok:
                    reason = "; ".join(verdict.reasons)
                    self.db.update_proposal(pid, status="rejected", reason=reason, decided_by=actor)
                    self.db.log("warn", f"Order {what} blocked at execution: {reason}")
                    return self.db.get_proposal(pid)

            # Claim only after the checks, so this order isn't counted against today's limit.
            if not self.db.claim_pending(pid):
                raise ValueError(f"proposal #{pid} is no longer pending")
            try:
                await self._send(pid, p, actor, what)
            except Exception as e:  # noqa: BLE001 - never leave a proposal stuck in "submitting"
                log.exception("order %s failed unexpectedly", what)
                cur = self.db.get_proposal(pid)
                if cur and cur["status"] == "submitting":
                    self.db.update_proposal(
                        pid, status="unknown", decided_by=actor,
                        reason=f"Unexpected error while sending ({type(e).__name__}: {e}). "
                               "Check open orders in Webull before retrying.")
                    self.db.log("error", f"Order {what} status UNKNOWN after an unexpected "
                                         f"error: {e}")
            if self.on_order:
                self.on_order()
            return self.db.get_proposal(pid)

    async def requote(self, pid: int, actor: str = "user") -> dict:
        """Rebuild a stale pending proposal at the current price without placing it.

        The owner sees the new numbers and has to approve a second time. Risk limits are
        re-checked against the new price; nothing is loosened or auto-executed.
        """
        async with self._exec_lock:
            p = self.db.get_proposal(pid)
            if p is None:
                raise ValueError(f"proposal #{pid} not found")
            if p["status"] != "pending":
                raise ValueError(f"proposal #{pid} is {p['status']}, not pending")
            if self.kill_switch and self._is_opening(p):    # M1: before any broker call
                raise ValueError(f"kill switch is on ({self.kill_reason or 'no new orders'}): "
                                 "a new order cannot be re-quoted")
            try:
                last = await self.broker.last_price(p["symbol"])
            except BrokerError as e:
                raise ValueError(f"Re-quote failed: no current price ({e})") from e
            if not last or last <= 0:
                raise ValueError(f"Re-quote failed: no current price for {p['symbol']}")
            previous = {"limit_price": p["limit_price"], "sl": p.get("sl"), "tp": p.get("tp"),
                        "ref_price": p.get("ref_price")}
            sig = self.db.get_signal(p["signal_id"]) if p.get("signal_id") else None
            limit = round(float(last), 2)
            sl, tp = ta.levels(limit, (sig or {}).get("atr"), self.s)
            fields: dict = {"limit_price": limit, "ref_price": limit,
                            "decided_by": None}
            if p["side"] == "BUY":
                fields.update({"sl": sl, "tp": tp})
            self.db.update_proposal(pid, **fields)
            fresh = self.db.get_proposal(pid)
            watchlist = self.research.effective_watchlist() if self.research else self.s.watchlist
            verdict = await self.risk.check(fresh, self.broker, self.db,
                                            kill_switch=self.kill_switch and self._is_opening(p),
                                            at_execution=True, watchlist=watchlist)
            self.db.update_proposal(pid, risk_checks=json.dumps(verdict.checks))
            self.db.log("info", f"Proposal #{pid} re-quoted at {limit:.2f} "
                                f"(was {previous['limit_price']:.2f}) for {actor}; "
                                + ("risk checks pass" if verdict.ok else
                                   "risk checks fail: " + "; ".join(verdict.reasons)))
            return {"proposal": self.db.get_proposal(pid), "previous": previous,
                    "risk_ok": verdict.ok, "reasons": verdict.reasons,
                    "risk_checks": verdict.checks}

    async def _send(self, pid: int, p: dict, actor: str, what: str) -> None:
        """Preview and place a claimed proposal. Always leaves a final status."""
        sym, sell = p["symbol"], p["side"] == "SELL"
        is_frac = self.s.fractional_enabled and isinstance(p["quantity"], float) and p["quantity"] != int(p["quantity"])
        is_option = _is_option_symbol(sym)

        if is_option:
            # Options path - use option order methods
            # Required option fields should already be validated in _on_proposal
            option_type = p.get("option_type")
            strike_price = p.get("strike_price")
            expiration_date = p.get("expiration_date")
            position_intent = p.get("position_intent", "BUY_TO_OPEN")

            closing = str(position_intent).upper().endswith("_TO_CLOSE")
            # EXIT_ORDER_TYPE=market: option closes go out as MARKET in the CORE session (the only
            # option session). A row pinned to "limit" (e.g. a patient take-profit) stays a limit.
            opt_type = ("MARKET" if closing and p.get("order_type") != "limit"
                        and exit_should_be_market(self.s, order_session()) else "LIMIT")
            # For options, limit_price is the premium per contract
            # Preview the option order
            try:
                preview = await self.broker.preview_option_order(
                    sym, p["side"], p["quantity"],
                    option_type, strike_price, expiration_date,
                    p["limit_price"], order_type=opt_type,
                    position_intent=position_intent
                )
            except BrokerError as e:
                if not closing:
                    self.db.update_proposal(pid, status="failed", reason=f"preview failed: {e}",
                                            decided_by=actor)
                    self.db.log("error", f"Option order {what} preview failed: {e}")
                    self._order_failed(e, what, "")   # preview: nothing was sent
                    return
                # Exits are never blocked: a preview is a courtesy, so a failing one is logged
                # and the closing order goes out anyway.
                preview = f"preview unavailable: {e}"
                self.db.log("warn", f"Option exit {what}: preview failed ({e}); sending anyway")

            # The coid is persisted BEFORE the broker call and reused on any re-send.
            coid = p.get("client_order_id") or _coid()
            self.db.update_proposal(pid, preview=preview, client_order_id=coid,
                                    decided_by=actor, order_type=opt_type.lower(),
                                    placed_at=now_utc().isoformat(timespec="seconds"))
            try:
                try:
                    response = await self.broker.place_option_single_order(
                        sym, p["side"], p["quantity"],
                        option_type, strike_price, expiration_date,
                        p["limit_price"], coid, order_type=opt_type,
                        position_intent=position_intent,
                        allow_when_killed=closing,
                    )
                except BrokerError as e0:
                    if opt_type != "MARKET" or e0.transport:
                        raise
                    if not market_rejection_is_definitive(e0):
                        # Ambiguous (HTTP 5xx / internal error): the MARKET may be live. Never send
                        # a second order; reconcile settles it by client_order_id.
                        self.db.log("error", f"{sym}: MARKET close error is ambiguous ({e0}); "
                                             "not re-sending; status set to unknown")
                        raise BrokerError(str(e0), transport=True) from e0
                    # Exits are never blocked: a rejected MARKET close is retried once as a limit.
                    self.db.log("warn", f"{sym}: MARKET close rejected ({e0}); re-sending as a limit")
                    coid = _coid()
                    self.db.update_proposal(pid, client_order_id=coid, order_type="limit")
                    response = await self.broker.place_option_single_order(
                        sym, p["side"], p["quantity"],
                        option_type, strike_price, expiration_date,
                        p["limit_price"], coid, order_type="LIMIT",
                        position_intent=position_intent,
                        allow_when_killed=closing,
                    )
            except BrokerError as e:
                if e.transport:
                    self.db.update_proposal(
                        pid, status="unknown", broker_response=str(e),
                        reason=f"No reply from broker. Check open orders in Webull for "
                               f"client_order_id {coid} before retrying.")
                    self.db.log("error", f"Option order {what} status UNKNOWN: {e}. The app will look it "
                                         "up at the broker and settle it automatically.")
                    self._order_failed(e, what, coid)
                else:
                    self.db.update_proposal(pid, status="failed", broker_response=str(e),
                                            reason="broker rejected the order")
                    self.db.log("error", f"Option order {what} rejected by broker: {e}")
                    self._order_failed(e, what, coid)
                return

            self.db.update_proposal(pid, status="placed", broker_response=response)
            self.order_failures = 0
            self._counted_transport = set()
            self.db.log("info", f"Option order {what} placed ({actor}). {response.splitlines()[-1]}", alert=True)
            return

        # Equity path (existing logic)
        # Resolve the session once: CORE (regular), ALL (premarket/after-hours) or NIGHT
        # (overnight). Outside every session (weekend/holiday) nothing can be sent.
        session = order_session()
        phase = session_phase()
        if session is None:
            self.db.update_proposal(
                pid, status="failed", decided_by=actor,
                reason="No trading session is open (weekend/holiday). Order not sent.")
            self.db.log("warn", f"Order {what} not sent: no trading session is open "
                                f"({phase}); the next session will be used.")
            return
        if session == "NIGHT" and (not self.s.overnight_trading
                                   or sym not in self.s.overnight_symbols):
            self.db.update_proposal(
                pid, status="failed", decided_by=actor,
                reason=(f"{sym} is not enabled for overnight trading. Add it to "
                        "OVERNIGHT_SYMBOLS (and set OVERNIGHT_TRADING=true) or wait for "
                        "the premarket session."))
            self.db.log("warn", f"Order {what} not sent: the overnight session only trades "
                                f"OVERNIGHT_SYMBOLS and {sym} is not listed.")
            return

        # Order style (market-orders feature). Default is LIMIT everywhere: today's behaviour.
        #   exits  : EXIT_ORDER_TYPE=market -> MARKET, CORE session only, whole shares only
        #   entries: ENTRY_ORDER_TYPE=marketable -> LIMIT at ask + offset (any session)
        order_type, style = "LIMIT", "limit"
        whole_qty = float(p["quantity"]) == int(p["quantity"])
        if (sell and whole_qty and p.get("order_type") != "limit"
                and exit_should_be_market(self.s, session)):
            order_type, style = "MARKET", "market"
        elif (not sell and not is_frac and self.s.entry_order_type == "marketable"):
            new_px = await self._marketable_entry_price(p)
            if new_px is not None:
                if abs(new_px - float(p["limit_price"])) > 1e-9:
                    self.db.log("info", f"{sym}: marketable entry limit {p['limit_price']:.4f} -> "
                                        f"{new_px:.4f}")
                p["limit_price"] = new_px
                self.db.update_proposal(pid, limit_price=new_px, quantity=p["quantity"])
                style = "marketable"
                what = f"#{pid} {p['side']} {p['quantity']} {sym} @ {new_px}"

        if not sell and session == "NIGHT":
            night_qty = overnight_scaled_qty(self.s, float(p["quantity"]), session)
            if night_qty != float(p["quantity"]):
                self.db.log("info", f"{sym}: overnight size factor {self.s.overnight_size_factor:g}: "
                                    f"qty {p['quantity']:g} -> {night_qty:g}")
                p["quantity"] = night_qty
                self.db.update_proposal(pid, quantity=night_qty)
                what = f"#{pid} {p['side']} {night_qty:g} {sym}"

        # For fractional orders, use AMOUNT entrust type with total_cash_amount
        entrust_type = "AMOUNT" if is_frac and not sell else "QTY"
        total_cash_amount = p["quantity"] * p["limit_price"] if is_frac and not sell else None

        try:
            preview = await self.broker.preview_order(sym, p["side"], p["quantity"],
                                                      p["limit_price"],
                                                      entrust_type=entrust_type,
                                                      total_cash_amount=total_cash_amount,
                                                      trading_session=session,
                                                      order_type=order_type)
        except BrokerError as e:
            self.db.update_proposal(pid, status="failed", reason=f"preview failed: {e}",
                                    decided_by=actor)
            self.db.log("error", f"Order {what} preview failed: {e}")
            self._order_failed(e, what, "")   # preview: nothing was sent
            return

        # The entry coid is reused when the row already carries one (a re-send must not mint a
        # second id); it is persisted below, before the first broker call that uses it.
        coids = {"entry": p.get("client_order_id") or _coid(), "tp": _coid(), "sl": _coid()}
        combo_id = _coid()
        # Bracket orders not supported for fractional
        # C16 (latent): bracket legs are sized to the REQUESTED qty, not the filled qty; a partial fill
        # leaves oversized legs. Unused live (BRACKET_TAKE_PROFIT=false => bracket is never True).
        # BRACKET_TAKE_PROFIT=false: no broker take-profit leg. A stop-only bracket is not a combo
        # the broker offers here, so the entry goes out alone and the protective GTC stop is
        # placed on fill (Portfolio._protect_filled_entry); winners are exited by the code rules.
        bracket = (not sell and self.s.bracket_orders and getattr(self.s, "bracket_take_profit", True)
                   and p.get("sl") and p.get("tp") and not is_frac)
        if bracket and session != "CORE":
            # Webull rejects the STOP_LOSS leg outside regular hours, so the combo would always
            # fail. Send the entry alone; the protective CORE stop is placed when it fills.
            bracket = False
            self.db.log("info", f"{sym}: {phase} session; bracket skipped (stop legs are only "
                                "accepted in the regular session). Entry sent as a single order; "
                                "the protective stop is placed on fill.")
        held_before, stops = 0.0, []
        cancel_partial: list = []
        if sell:
            # Stops and targets this app placed must go first: they would otherwise be able
            # to sell the same shares again after this order fills.
            try:
                if self.portfolio is not None:
                    await self.portfolio.sync_statuses()
                held_before = (await fresh_positions(self.broker)).get(sym, 0.0)
                stops = await self._cancel_protective(sym, coids["entry"], cancel_partial)
            except BrokerError as e:
                stops = None
                self.db.log("error", f"Order {what}: {e}")
            if stops is None:
                self.db.update_proposal(pid, status="failed", decided_by=actor,
                                        reason=f"could not clear the protective orders for {sym}; "
                                               "sell not sent")
                partial = list(cancel_partial)
                if partial:   # C4: stops already cancelled must go back on
                    await self._reprotect_partial(p, held_before, partial)
                    self.db.settle_stops(coids["entry"], "sell not sent; stop protection restored")
                return

            # A protective stop can fill while its cancellation is in flight. The earlier
            # risk check and held_before snapshot then no longer reflect the position we can
            # safely sell. Refresh after cancellation and fail closed if that read is stale.
            try:
                held_now = (await fresh_positions(self.broker)).get(sym, 0.0)
            except BrokerError as e:
                self.db.update_proposal(pid, status="failed", decided_by=actor,
                                        reason=f"could not recheck {sym} holdings after clearing "
                                               "protective orders; sell not sent")
                if stops:
                    await self._reprotect(p, held_before, stops)
                self.db.settle_stops(coids["entry"], "sell not sent; stop protection restored")
                self.db.log("error", f"Order {what}: holdings recheck failed after protective "
                                    f"orders were cleared: {e}")
                return
            if p["quantity"] > held_now:
                self.db.update_proposal(
                    pid, status="failed", decided_by=actor,
                    reason=f"holdings changed while clearing protective orders: account now "
                           f"holds {held_now:g} {sym}; sell not sent")
                if stops:
                    await self._reprotect(p, held_now, stops)
                self.db.settle_stops(coids["entry"], "sell not sent; stop protection restored")
                self.db.log("warn", f"Order {what} blocked after clearing protective orders: "
                                    f"account now holds {held_now:g} {sym}")
                return
            held_before = held_now

        self.db.update_proposal(pid, preview=preview, client_order_id=coids["entry"],
                                decided_by=actor, order_type=style,
                                placed_at=now_utc().isoformat(timespec="seconds"))
        sell_tag = coids["entry"]       # the coid the cancelled stops were tagged with
        # Persist every id of the bracket (entry on the proposal row above; take-profit, stop and
        # the combo id in the protective rows) before the broker sees any of them.
        bracket_rows: list[int] = (self._record_bracket(pid, p, coids, status="unknown",
                                                         combo_id=combo_id) if bracket else [])
        try:
            if bracket:
                try:
                    response = await self.broker.place_bracket(
                        sym, p["quantity"], p["limit_price"], p["tp"], p["sl"], coids,
                        combo_id, trading_session=session)
                except BrokerError as e:
                    # A combo rejection is atomic: nothing was placed. Retry the entry as a
                    # single order in the same session only when the session is what the
                    # broker rejected (e.g. exits not offered in the overnight session).
                    if e.transport or not _session_rejection(e):
                        raise
                    bracket = False
                    self._settle_bracket_rows(bracket_rows, "closed",
                                              "bracket rejected; entry re-sent as a single order")
                    bracket_rows = []
                    self.db.log(
                        "warn",
                        f"Broker rejected the bracket for {sym} in the {phase} session "
                        f"({e}); placing the {session} entry as a single order. It will "
                        "have no broker-side stop/target until the next regular session, "
                        "but the app's exit rules still apply.", alert=True)
                    response = await self.broker.place_order(
                        sym, p["side"], p["quantity"], p["limit_price"], coids["entry"],
                        entrust_type=entrust_type, total_cash_amount=total_cash_amount,
                        trading_session=session)
            else:
                try:
                    response = await self.broker.place_order(
                        sym, p["side"], p["quantity"], p["limit_price"], coids["entry"],
                        entrust_type=entrust_type, total_cash_amount=total_cash_amount,
                        trading_session=session, order_type=order_type)
                except BrokerError as e0:
                    if order_type != "MARKET" or e0.transport:
                        raise
                    if not market_rejection_is_definitive(e0):
                        # Ambiguous (HTTP 5xx / internal error): the MARKET may be live. Never send
                        # a second SELL; reconcile settles it by client_order_id.
                        self.db.log("error", f"{sym}: MARKET sell error is ambiguous ({e0}); "
                                             "not re-sending; status set to unknown")
                        raise BrokerError(str(e0), transport=True) from e0
                    # Exits are never blocked: a rejected MARKET sell is retried once as a limit.
                    self.db.log("warn", f"{sym}: MARKET sell rejected ({e0}); re-sending as a limit")
                    order_type, style = "LIMIT", "limit"
                    coids["entry"] = _coid()
                    self.db.update_proposal(pid, client_order_id=coids["entry"], order_type="limit")
                    self.db.retag_stops(sell_tag, coids["entry"])
                    sell_tag = coids["entry"]
                    response = await self.broker.place_order(
                        sym, p["side"], p["quantity"], p["limit_price"], coids["entry"],
                        entrust_type=entrust_type, total_cash_amount=total_cash_amount,
                        trading_session=session, order_type="LIMIT")
        except BrokerError as e:
            if e.transport:
                # a bracket's legs may exist: their rows stay "unknown" for the status sync
                self.db.update_proposal(
                    pid, status="unknown", broker_response=str(e),
                    reason=f"No reply from broker. Check open orders in Webull for "
                           f"client_order_id {coids['entry']} before retrying.")
                self.db.log("error", f"Order {what} status UNKNOWN: {e}. The app will look it "
                                     "up at the broker and settle it automatically.")
                self._order_failed(e, what, coids["entry"])
                if sell and stops:
                    self.db.log("error", f"The {sym} stop was cancelled for a sell whose status "
                                         "is unknown. It is restored automatically if the sell "
                                         "turns out not to have gone through; check Webull.")
            else:
                self._settle_bracket_rows(bracket_rows, "closed", "broker rejected the bracket")
                self.db.update_proposal(pid, status="failed", broker_response=str(e),
                                        reason="broker rejected the order")
                self.db.log("error", f"Order {what} rejected by broker: {e}")
                self._order_failed(e, what, coids["entry"])
                if sell and stops:   # the sell did not happen: put the stop back on every share
                    await self._reprotect(p, held_before, stops)
                    self.db.settle_stops(coids["entry"], "sell rejected; stop restored")
            return

        self.db.update_proposal(pid, status="placed", broker_response=response)
        self.order_failures = 0
        self._counted_transport = set()
        if bracket:
            self._settle_bracket_rows(bracket_rows, "waiting", "waiting for the entry to fill")
            self.db.log("info", f"Order {what} placed as a bracket ({actor}): stop "
                                f"{p['sl']}, target {p['tp']}.", alert=True)
        else:
            self.db.log("info", f"Order {what} placed ({actor}). {response.splitlines()[-1]}",
                        alert=True)
        if sell and stops:
            await self._reprotect(p, held_before - p["quantity"], stops)
        # Record trade ledger entry for BUYs (entries) and SELLs (exits)
        if p["side"] == "BUY":
            sig = self.db.get_signal(p.get("signal_id")) if p.get("signal_id") else None
            self.db.add_trade_entry(pid, p, sig)

    # -- order-failure accounting (S4/S16) ------------------------------------------
    def order_send_blocked(self) -> bool:
        """True while a session rejection blocks new (opening) orders in the current session."""
        blocked = getattr(self, "_session_block", None)
        if blocked is None:
            return False
        key = (now_et().date().isoformat(), session_phase())
        if key != blocked:
            self._session_block = None
            return False
        return True

    def uncount_failure(self, coid: str) -> None:
        """Reconcile found the order this transport failure was counted for: take it back."""
        pending = getattr(self, "_counted_transport", None)
        if pending and coid in pending:
            pending.discard(coid)
            self.order_failures = max(0, self.order_failures - 1)

    def _order_failed(self, e: BrokerError, what: str, coid: str) -> None:
        """Classify a failed order and count it only when it is a definite order rejection
        (or a transport timeout, un-counted later by uncount_failure). The threshold is checked
        here so a burst trips at once via the ``error_trip_cb`` hook the supervisor installs."""
        kind = classify_order_error(e)
        if kind == "session":
            self._session_block = (now_et().date().isoformat(), session_phase())
            self.db.log("warn", f"{what}: session rejection ({e}); new orders paused until the "
                                "session changes (not counted as a failure)", alert=True)
            return
        if kind == "auth":
            self.db.log("error", f"BROKER AUTH problem on {what}: {e}. Check credentials/token "
                                 "(not counted as a failed order)", alert=True)
            return
        if kind == "rate_limit":
            self.db.log("warn", f"{what}: rate limit exhausted ({e}); not counted as a failure")
            return
        if kind == "transport" and not coid:
            # e.g. a preview timeout: nothing was sent, so there is no order to reconcile/uncount
            self.db.log("warn", f"{what}: no reply before sending ({e}); not counted as a failure")
            return
        if kind == "transport":
            if not hasattr(self, "_counted_transport"):
                self._counted_transport = set()
            self._counted_transport.add(coid)
        self._failed("order", f"no reply for {what}" if kind == "transport"
                     else f"broker rejected {what}: {e}")
        n = self.s.kill_max_consecutive_errors
        cb = getattr(self, "error_trip_cb", None)
        # Instant trip only on DEFINITE rejections: "no reply" timeouts may still be confirmed as
        # placed by reconcile (uncount_failure), so they are left to the supervisor's tick, which
        # runs reconcile first. A broker that stays dead still trips on that tick.
        definite = self.order_failures - len(getattr(self, "_counted_transport", ()) or ())
        if n and cb and not self.kill_switch and kind != "transport" and definite >= n:
            cb(f"{self.order_failures} failed orders in a row (last: {self.last_failure})")

    async def _marketable_entry_price(self, p: dict) -> float | None:
        """ENTRY_ORDER_TYPE=marketable: the BUY limit for ``p`` right now, or None to keep the
        proposed limit. The price is ask (else last) x (1 + offset), capped at the price band from
        the proposal's reference price. It is not used when it would push the order over
        MAX_ORDER_USD or put the stop at/above the entry."""
        sym = p["symbol"]
        try:
            q = await self.broker.quote(sym)
        except BrokerError as e:
            self.db.log("warn", f"{sym}: no quote for a marketable entry ({e}); keeping the limit")
            return None
        if q is None:
            return None
        px = marketable_buy_price(self.s, last=q.last, ask=q.ask,
                                  ref_price=p.get("ref_price") or p["limit_price"])
        if px is None:
            return None
        # Re-run the order-value / stop / target / risk-per-trade / position checks at the NEW price.
        try:
            held = (await fresh_positions(self.broker)).get(sym, 0.0)
        except BrokerError:
            return None
        why = reprice_violation(self.s, qty=float(p["quantity"]), px=px, sl=p.get("sl"),
                                tp=p.get("tp"), held=held, last=q.last or px)
        if why:
            # C2: sizing fills a cap exactly, so the higher marketable price nearly always breaks
            # it. Shrink to the largest whole-share quantity allowed at the new price instead.
            nq = int(max_allowed_qty(self.s, limit_price=px, last_price=q.last or px, held=held,
                                     stop_loss=p.get("sl"), max_qty_override=float(p["quantity"])))
            if nq < 1 or reprice_violation(self.s, qty=float(nq), px=px, sl=p.get("sl"),
                                           tp=p.get("tp"), held=held, last=q.last or px):
                self.db.log("info", f"{sym}: marketable entry price {px} not used: {why}")
                return None
            self.db.log("info", f"{sym}: marketable entry at {px}: quantity {p['quantity']:g} -> "
                                f"{nq} to stay within the limits")
            p["quantity"] = float(nq)       # caller persists it with the new limit price
        return px

    def _record_bracket(self, pid: int, p: dict, coids: dict, status: str,
                        combo_id: str | None = None) -> list[int]:
        note = f"combo {combo_id}" if combo_id else ""
        return [
            self.db.add_protective(pid, p["symbol"], "tp", coids["tp"], p["quantity"], p["tp"],
                                   status, note),
            self.db.add_protective(pid, p["symbol"], "sl", coids["sl"], p["quantity"], p["sl"],
                                   status, note),
        ]

    def _settle_bracket_rows(self, ids: list[int], status: str, note: str) -> None:
        for rid in ids:
            try:
                self.db.set_protective(rid, status, note)
            except Exception:  # noqa: BLE001 - bookkeeping must not break order handling
                log.exception("could not update protective row %s", rid)

    async def _cancel_protective(self, symbol: str, sell_coid: str,
                                 partial: list | None = None) -> list[float] | None:
        """Cancel this app's armed stop/target orders for a symbol before selling it.

        Returns the stop prices that were cancelled, or None if a cancel got no reply
        (then the sell must not be sent). Legs still waiting for their own entry to fill
        are left alone: they can't sell anything yet. Each cancelled stop remembers the
        sell it made room for, so it can be restored if that sell never fills.
        """
        stops: list[float] = []
        # On failure (None) the (price, quantity) of stops already cancelled are left in the
        # caller-owned `partial` list so it can re-protect exactly those shares (C4). The list is
        # per call: the risk loop runs soft stops without _exec_lock, so shared state would race.
        if partial is None:
            partial = []
        else:
            partial.clear()
        for o in self.db.protective(symbol):
            if o["status"] == "waiting":
                continue
            if o["status"] == "unknown":
                # C3: a stop whose send timed out may never have reached the broker.
                try:
                    found = await self._order_status(o["client_order_id"])
                except Exception as e:  # noqa: BLE001 - cannot tell: fail closed
                    self.db.log("error", f"Cannot look up the {symbol} protective order: {e}")
                    return None
                if found is None:
                    if _row_age_seconds(o) <= max(
                            300.0, float(getattr(self.s, "reconcile_give_up_minutes", 15)) * 60):
                        self.db.log("warn", f"{symbol} protective order not listed yet "
                                            "(too young to give up on); sell not sent")
                        return None
                    self.db.set_protective(o["id"], "closed", "never reached the broker")
                    continue
            try:
                reply = await self.broker.cancel_order(o["client_order_id"])
                if not cancel_reply_confirmed(reply):
                    # "Order not found; it may have been filled..." / "Permission denied": the
                    # stop may still be live or may have filled. Do not sell on top of it.
                    self.db.log("error", f"Cancel of the {symbol} protective order not confirmed "
                                         f"({str(reply)[:80]}); sell not sent")
                    return None
            except BrokerError as e:
                if e.transport:
                    kind = "stop" if o["kind"] == "sl" else "target"
                    self.db.log("error", f"Cancel of the {symbol} {kind} got no reply: {e}")
                    return None
                # C5: the broker refused the cancel. Only a status showing it already cancelled
                # lets the sell go; live, filled or unknown means no sell.
                try:
                    st = await self._order_status(o["client_order_id"])
                except Exception:  # noqa: BLE001
                    st = "?"
                if st is None:
                    # In neither open orders nor history: it is not live (never reached the broker).
                    self.db.set_protective(o["id"], "closed", "cancel refused; not at the broker")
                    continue
                if st not in DONE_CANCELLED:
                    self.db.log("error", f"Cancel of the {symbol} protective order refused ({e}); "
                                         "sell not sent")
                    return None
            self.db.set_protective(o["id"], "cancelled", "cancelled before a sell",
                                   sell_coid=sell_coid if o["kind"] == "sl" else None)
            if o["kind"] == "sl":
                stops.append(o["price"])
                partial.append((o["price"], float(o.get("quantity") or 0)))
        return stops

    async def _order_status(self, coid: str) -> str | None:
        """Upper-case broker status of this client order id from open orders, then history;
        None when neither lists it. Raises on a failed read."""
        for text in (await self.broker.open_orders_text(), await self.broker.order_history_text()):
            for o in parse_orders(text):
                if o.get("client_order_id") == coid:
                    return (o.get("status") or "").upper() or "?"
        return None

    async def _reprotect_partial(self, p: dict, held: float, partial: list) -> None:
        """C4: put back only the shares of the stops actually cancelled (capped at holdings),
        at the tightest of their prices."""
        if not partial:
            return
        shares = min(sum(q for _, q in partial), held)
        await self._reprotect(p, shares, [px for px, _ in partial])

    async def _reprotect(self, p: dict, shares: float, stops: list[float]) -> None:
        """Put a stop back on `shares` (what's left after a partial sell, or everything
        after a sell the broker rejected), at the tightest of the cancelled stop prices."""
        if not stops or shares <= 0:
            return
        stop = max(stops)
        coid = _coid()
        # persist the coid first ("unknown" until the broker answers), then send
        row = self.db.add_protective(p["id"], p["symbol"], "sl", coid, shares, stop, "unknown",
                                     "re-protects shares after a sell")
        try:
            try:
                await self.broker.place_stop(p["symbol"], shares, stop, coid, allow_when_killed=True)
            except BrokerError as e0:
                # timed out ("outcome unknown"): adopt the order if it is already live
                if not (e0.transport and await self._open_order_exists(coid)):
                    raise
            self.db.set_protective(row, "active", "re-protects shares after a sell")
            self.db.log("info", f"Stop placed for {shares:g} {p['symbol']} at {stop}")
        except BrokerError as e:
            if not e.transport:
                self.db.set_protective(row, "closed", f"stop not placed: {e}")
            self.db.log("error", f"Could not re-protect {shares:g} {p['symbol']} with a stop "
                                 f"at {stop}: {e}. Set one in Webull.")
