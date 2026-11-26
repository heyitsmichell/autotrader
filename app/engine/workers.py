"""Cold-path background workers (QA M4).

Everything slow and network-bound that feeds a decision runs here, never under ``_run_lock``:

  * signals worker      TA + sentiment + ML scoring for the watchlist (``SignalService.refresh``),
                        written to the TTL-cached ``SignalStore`` (``engine.signal_store``);
  * research worker     ``research.refresh_if_due()`` (only when the research service has no scan
                        loop of its own running);
  * maintenance worker  daily ``db.prune(DB_RETENTION_DAYS)`` and, when options are enabled, the
                        once-a-day ATM IV recording that feeds the IV rank.

The slow cycle and the fast loop only READ the store; a stalled worker therefore makes them fail
closed (no fresh signal, no new BUY) instead of trading on old data.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..market import market_is_open, market_is_open_extended, now_et, order_session

log = logging.getLogger("app.engine")

TICK_SECONDS = 30.0
MAINTENANCE_SECONDS = 900.0


class Workers:
    def __init__(self, engine) -> None:
        self.eng = engine
        self._tasks: list[asyncio.Task] = []
        self.errors: dict[str, str] = {}
        self.last_prune: dict | None = None
        self._first_signal_pass = True
        self._ml_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    def start(self) -> None:
        if self.running:
            return
        self._tasks = [
            asyncio.create_task(self._forever("signals", self.signals_pass, TICK_SECONDS, 2.0),
                                name="worker-signals"),
            asyncio.create_task(self._forever("research", self.research_pass, 60.0, 5.0),
                                name="worker-research"),
            asyncio.create_task(self._forever("options_ml", self.options_ml_pass, 3600.0, 20.0),
                                name="worker-options-ml"),
            asyncio.create_task(self._forever("maintenance", self.maintenance_pass,
                                              MAINTENANCE_SECONDS, 10.0),
                                name="worker-maintenance"),
            asyncio.create_task(self._forever("scanner_log", self.scanner_log_pass, 900.0, 60.0),
                                name="worker-scanner-log"),
        ]
        self.eng.db.log("info", "Cold-path workers started (signals, research, maintenance)")

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        if tasks:
            self.eng.db.log("info", "Cold-path workers stopped")

    async def _forever(self, name: str, fn, every: float, first_delay: float) -> None:
        await asyncio.sleep(first_delay)
        while True:
            try:
                await fn()
                self.errors.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a worker must survive any failure
                self.errors[name] = f"{type(e).__name__}: {e}"
                log.exception("%s worker pass failed", name)
            await asyncio.sleep(every)

    def status(self) -> dict:
        eng = self.eng
        return {"running": self.running, "errors": dict(self.errors),
                "signal_store_size": len(eng.signal_store),
                "signal_store_newest_age": eng.signal_store.newest_age(),
                "last_prune": self.last_prune}

    # -- signals -------------------------------------------------------------
    def _session_ok(self) -> bool:
        s = self.eng.s
        if s.market_hours_only:
            return market_is_open()
        if s.overnight_trading:
            return order_session() is not None
        return market_is_open_extended()

    async def refresh_signals_now(self) -> list[dict]:
        """Refresh the watchlist's signals and publish them to the store."""
        eng = self.eng
        watchlist = eng.research.effective_watchlist() if eng.research else eng.s.watchlist
        sigs = await eng.signals.refresh(symbols=list(watchlist), with_llm=False)
        eng.signal_store.put_many(sigs)
        return sigs

    async def signals_pass(self) -> bool:
        """One tick of the signals worker. True when it refreshed."""
        eng = self.eng
        minutes = int(getattr(eng.s, "signal_refresh_minutes", 0) or 0)
        if minutes <= 0 or not eng.broker.connected or eng.signals.refreshing:
            return False
        age = eng.signal_store.newest_age()
        due = age is None or age >= minutes * 60 * 0.9
        if not due:
            return False
        if not (self._first_signal_pass or self._session_ok()):
            return False
        await self.refresh_signals_now()
        self._first_signal_pass = False
        return True

    # -- research ------------------------------------------------------------------
    async def research_pass(self) -> bool:
        research = self.eng.research
        if research is None or getattr(research, "_task", None) is not None:
            return False        # the service runs its own scan loop
        await research.refresh_if_due()
        return True

    # -- options ML retrain (background thread, never blocks trading) -------------------
    async def options_ml_pass(self) -> bool:
        """Train the options model when it is missing or older than OPTIONS_ML_RETRAIN_DAYS."""
        eng = self.eng
        if not eng.s.options_ml_enabled:
            return False
        from .. import options_ml
        live = await asyncio.to_thread(options_ml.live_training_rows, eng.db)
        if not options_ml.retrain_due(eng.s, n_live=len(live)):
            return False
        model = await asyncio.to_thread(options_ml.train, live,
                                        live_weight=eng.s.options_ml_live_weight)
        if model is None:
            return False
        eng.db.log("info", options_ml.report_line(model))
        return True

    # -- read-only scanner history (no orders) ---------------------------------------
    async def scanner_log_pass(self) -> dict:
        from ..scanner_log import log_scanners
        return await log_scanners(self.eng)

    # -- maintenance ---------------------------------------------------------------
    async def maintenance_pass(self) -> dict:
        """Daily DB prune, and the ATM IV recording for the allowed underlyings."""
        eng = self.eng
        done: dict = {}
        today = now_et().date().isoformat()
        if eng.db.get_kv("db_prune_day") != today:
            days = int(getattr(eng.s, "db_retention_days", 90) or 90)
            removed = await asyncio.to_thread(eng.db.prune, days)
            eng.db.set_kv("db_prune_day", today)
            self.last_prune = {"day": today, "retention_days": days, "removed": removed}
            done["prune"] = removed
            eng.db.log("info", f"DB prune (retention {days} d): "
                               + ", ".join(f"{k} -{v}" for k, v in removed.items()))
        if eng.s.options_enabled and eng.broker.connected:
            recorded = {}
            for underlying in eng.s.options_allowed_underlyings:
                try:
                    iv = await asyncio.wait_for(eng.record_atm_iv(underlying), 30)
                except Exception as e:  # noqa: BLE001
                    log.debug("ATM IV for %s skipped: %r", underlying, e)
                    continue
                if iv is not None:
                    recorded[underlying] = iv
            if recorded:
                done["atm_iv"] = recorded
        return done


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # typing-only host contract; no runtime base class
    from ._host import WorkersHost as _Host
else:
    _Host = object

class WorkersMixin(_Host):
    async def start_workers(self) -> None:
        self.workers.start()

    async def stop_workers(self) -> None:
        await self.workers.stop()
