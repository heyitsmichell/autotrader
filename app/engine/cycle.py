"""The slow deterministic cycle.

Hot/cold split (QA M4): everything that talks to the network for research, news, sentiment, ML
scoring and yfinance runs OUTSIDE ``_run_lock``, either in the background workers (which keep
``signal_store`` warm) or, when the store is empty/stale or the run is manual, at the start of
``run_cycle`` before the lock is taken. The lock only covers proposal generation from signals
that are already in hand.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..broker import BrokerError
from ..market import now_utc

log = logging.getLogger("app.engine")


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing-only host contract; no runtime base class
    from ._host import CycleHost as _Host
else:
    _Host = object

class CycleMixin(_Host):
    # How long a cycle waits for a cold-path refresh before it carries on without it.
    COLD_REFRESH_TIMEOUT_SECONDS = 120.0

    def _watchlist(self) -> list[str]:
        return list(self.research.effective_watchlist() if self.research else self.s.watchlist)

    def _warn_read_failure(self, what: str, err: Exception) -> None:
        now = time.monotonic()
        if now - getattr(self, "_read_fail_logged_at", -1e9) >= 300:
            self._read_fail_logged_at = now
            log.warning("broker %s read failed (%r); assuming there is work", what, err)

    async def _has_actionable_work(self, sigs: list[dict] | None = None) -> bool:
        """Check if there's anything actionable that would justify a cycle.

        Returns True if:
        - We have any held positions (need to manage exits)
        - We have any pending/working orders (need to monitor)
        - Any symbol on the watchlist has a BUY gate that could pass (TA, sentiment, ML all positive)

        ``sigs`` are the signals the cycle already holds. This never fetches signals itself
        (that would be network I/O), it falls back to the signal store.
        """
        # Check held positions
        try:
            positions = await self.broker.positions()
            if any(qty > 0 for qty in positions.values()):
                return True
        except Exception as e:  # noqa: BLE001 - failed read must not skip exit management
            self._warn_read_failure("positions", e)
            return True

        # Check pending/working orders
        pending = self.db.pending_proposals()
        if pending:
            return True

        # Check working orders at broker
        try:
            open_text = await self.broker.open_orders_text()
            if open_text and not open_text.startswith("No data"):
                # Has some working orders
                return True
        except Exception as e:  # noqa: BLE001
            self._warn_read_failure("open orders", e)
            return True

        # Check if any symbol could potentially pass the BUY gate
        try:
            if sigs is None:
                sigs = self.signal_store.snapshot(self._watchlist())
            for sig in sigs:
                if sig.get("consensus") == "long":
                    return True
        except Exception as exc:
            log.warning("signal snapshot check failed: %s", exc)
            pass

        return False

    async def _gather_signals(self, run_id: int, trigger: str) -> list[dict]:
        """Signals for this cycle, obtained OUTSIDE ``_run_lock``.

        A manual run always refreshes (research then signals), as ``/api/run`` always did. A
        scheduled run reads the warm ``signal_store``; when it is empty or stale it refreshes
        first, waiting at most ``COLD_REFRESH_TIMEOUT_SECONDS`` and then carrying on with
        whatever fresh signals exist (never with stale ones: no fresh signal means no BUY).
        """
        watchlist = self._watchlist()
        store = self.signal_store
        fresh = store.snapshot(watchlist)
        need_refresh = trigger == "manual" or not fresh
        if need_refresh:
            if self.research is not None:
                try:
                    await asyncio.wait_for(self.research.refresh_if_due(),
                                           self.COLD_REFRESH_TIMEOUT_SECONDS)
                except Exception as e:  # noqa: BLE001 - stale research must not halt the run
                    self.db.log("warn", f"Automated research refresh failed: {e!r}")
            try:
                sigs = await asyncio.wait_for(self.signals.refresh(run_id, symbols=watchlist),
                                              self.COLD_REFRESH_TIMEOUT_SECONDS)
                store.put_many(sigs)
                self.db.log("info", f"Signals refreshed for {len(sigs)} symbol(s): "
                            + ", ".join(f"{x['symbol']} {x['consensus']}" for x in sigs))
            except Exception as e:  # noqa: BLE001
                self.db.log("warn", f"Signal refresh failed: {e!r}")
            fresh = store.snapshot(watchlist)
        # Held positions need a recent signal for exit decisions: fetch missing ones here, not
        # inside the lock (signals.ensure refreshes over the network when its row is stale).
        try:
            positions = await self.broker.positions()
        except Exception as exc:  # noqa: BLE001
            log.warning("positions read failed in cycle (assuming none): %s", exc)
            positions = {}
        for symbol, qty in positions.items():
            if qty > 0:
                try:
                    await asyncio.wait_for(self.signals.ensure(symbol, max_age_minutes=60),
                                           self.COLD_REFRESH_TIMEOUT_SECONDS)
                except Exception as e:  # noqa: BLE001
                    log.debug("pre-warm signal for %s failed: %r", symbol, e)
        return fresh

    # -- AI runs -------------------------------------------------------------
    async def run_cycle(self, trigger: str) -> int | None:
        """Run a deterministic trading cycle (no LLM in the hot path).

        Phase 6: LLM removed from execution path. Trade decisions are made by:
        - Consensus gate (TA + sentiment + ML filter)
        - Risk checks
        - Core-satellite rebalancing rules

        The LLM is only used for offline research (research service).
        """
        if self.kill_switch:
            self.db.log("warn", "Run skipped: kill switch is on")
            return None
        if self._run_lock.locked() or self._cycle_active:
            self.db.log("warn", "Run skipped: another run is in progress")
            return None

        # Enforce minimum cycle interval (prevents excessive API calls)
        now = now_utc()
        if (self._last_llm_run and
            trigger == "scheduler" and
            (now - self._last_llm_run).total_seconds() < self._min_llm_cycle_seconds):
            remaining = self._min_llm_cycle_seconds - (now - self._last_llm_run).total_seconds()
            self.db.log("info", f"Run skipped: minimum cycle interval ({self._min_llm_cycle_seconds}s) not met, {remaining:.0f}s remaining")
            return None

        self._cycle_active = True
        try:
            run_id = self.db.create_run(trigger, "deterministic")
            # Cold path first, without the lock.
            sigs = await self._gather_signals(run_id, trigger)

            # Deterministic pre-check: skip if nothing is actionable
            if not await self._has_actionable_work(sigs):
                self.db.finish_run(run_id, "skipped", summary="nothing actionable: no positions, no pending orders, all BUY gates blocked")
                self.db.log("info", f"Run #{run_id} skipped: nothing actionable")
                return run_id

            async with self._run_lock:
                self._last_llm_run = now_utc()
                self.db.log("info", f"Deterministic run #{run_id} started ({trigger})")
                self.save_run_params(run_id)

                # Generate proposals deterministically (from signals already in hand)
                proposals_generated = 0
                gen_failed = False
                try:
                    proposals_generated = await self._generate_proposals_deterministic(run_id, sigs)
                except Exception as e:  # noqa: BLE001
                    gen_failed = True
                    log.exception("deterministic proposal generation failed")
                    self.db.log("error", f"Proposal generation failed: {type(e).__name__}: {e}")
                    self._failed("run", f"proposal generation failed: {type(e).__name__}: {e}")

                if not gen_failed:
                    self.run_failures = 0
                # No LLM cost for deterministic runs
                self.db.finish_run(
                    run_id, "ok", summary=f"Deterministic run: {proposals_generated} proposal(s) generated", tool_calls=[],
                    input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, cost_usd=0.0,
                )
                self.db.log("info", f"Deterministic run #{run_id} finished: {proposals_generated} proposal(s) generated")
        finally:
            self._cycle_active = False

        await self._auto_execute_run(run_id)
        return run_id

    def _fail_run(self, run_id: int, message: str) -> int:
        self.db.finish_run(run_id, "error", error=message)
        self.db.log("error", f"Run #{run_id} failed: {message}")
        self._failed("run", message)
        return run_id
