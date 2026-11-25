"""Supervisor: the background loop that lets the app run without you watching.

Every WATCHDOG_SECONDS it:
  1. Keeps the broker connected. If the Webull MCP server dies or the connection
     drops, it reconnects with a growing pause (30 s up to 10 min), and alerts you
     if the broker stays down for BROKER_DOWN_ALERT_MINUTES.
  2. Settles orders whose status is unknown (engine.reconcile_orders).
  3. Starts the scheduler when AUTO_START_SCHEDULER=true, unless you stopped it
     yourself or the kill switch is on.
  4. Trips the kill switch automatically on:
       - drawdown: net liquidation KILL_MAX_DRAWDOWN_PCT below its highest value
       - daily loss: day P&L at or below -KILL_DAILY_LOSS_USD
       - errors: KILL_MAX_CONSECUTIVE_ERRORS failed AI runs, or failed/unknown
         orders, in a row
     then does KILL_ACTION: pause (stop trading), cancel_entries (also cancel
     working BUY orders, keep stops and targets) or flatten (cancel everything and
     sell all positions). Releasing the kill switch is always up to you.
  5. Warns you before the Webull login expires (TOKEN_WARN_DAYS), and when it
     needs re-approval.
  6. Sends a daily summary after the close (ALERT_DAILY_SUMMARY).

Deposits and withdrawals move net liquidation too: a large withdrawal can trip
the drawdown trigger. Releasing the kill switch re-bases the peak to the current
value.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from .alerts import Alerts
from .broker import Broker, BrokerError
from .config import Settings
from .db import DB
from .engine import Engine
from .learning import Learner
from .logging_setup import trim_shared_logs
from .market import ET, now_et, now_utc, session_close, start_of_today_et_utc
from .portfolio import Portfolio

log = logging.getLogger(__name__)

AUTH_PATTERNS = re.compile(r"token|2FA|PENDING|Device Not Registered|401|403|unauthori[sz]ed|"
                           r"auth", re.I)
TOKEN_LIFETIME_DAYS = 15
SUMMARY_DELAY_MIN = 10


class Supervisor:
    def __init__(self, settings: Settings, db: DB, broker: Broker, engine: Engine,
                 portfolio: Portfolio, alerts: Alerts, learner: Learner | None = None) -> None:
        self.s, self.db, self.broker, self.engine = settings, db, broker, engine
        self.portfolio, self.alerts = portfolio, alerts
        self.learner = learner
        self.down_since: float | None = None
        self._down_alerted = False
        self._backoff = 30.0
        self._next_reconnect = 0.0
        self._last_token_check = 0.0
        self.token: dict | None = None
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self.drawdown_pct: float | None = None
        self._task: asyncio.Task | None = None
        self._tick_count = 0
        self._wal_checkpoint_interval = 60  # checkpoint WAL every 60 ticks
        # Core-satellite rebalance check interval (every N ticks)
        self._core_rebalance_interval = 5  # every ~2.5 minutes at 30s watchdog
        self._last_core_rebalance: float = 0.0
        # Protective stop check interval (every N ticks)
        self._protective_stops_interval = 5  # every ~2.5 minutes at 30s watchdog
        # SPY benchmark cache
        self._spy_benchmark_cache: dict | None = None
        self._spy_status_cache: dict | None = None  # dashboard-only; different shape from the cache above
        self._spy_benchmark_cache_time: float = 0.0
        self._spy_benchmark_cache_ttl: float = 300.0  # 5 minutes cache
        # Daily performance row (Phase 0), checked at most every few minutes.
        self._last_benchmark_check: float = 0.0
        self._benchmark_day: str | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="supervisor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        last_trim = 0.0
        while True:
            if time.monotonic() - last_trim >= 720:  # ~12 min; a stat call per file
                last_trim = time.monotonic()
                trim_shared_logs()
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log.exception("supervisor tick")
                self.last_error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(self.s.watchdog_seconds)

    async def tick(self) -> None:
        self.last_tick = now_utc().isoformat(timespec="seconds")
        await self.broker_health()
        if self.broker.connected:
            await self.engine.reconcile_orders()
            self.auto_start_scheduler()
            await self.check_kill_triggers()
            await self.check_spy_benchmark()
            await self.daily_benchmark()
            # Core-satellite rebalance check (periodic)
            if self._tick_count % self._core_rebalance_interval == 0:
                try:
                    await self.engine.check_core_rebalance()
                except Exception as e:
                    self.db.log("warn", f"Core rebalance check failed: {e}")
            # Periodic protective stop verification: ensure held equities have working stop orders
            if self._tick_count % self._protective_stops_interval == 0:
                try:
                    await self.engine._verify_protective_stops()
                except Exception as e:
                    self.db.log("warn", f"Protective stop verification failed: {e}")
        self.check_token()
        await self.daily_summary()
        # Periodic WAL checkpoint to keep db-wal size bounded
        self._tick_count += 1
        if self._tick_count % self._wal_checkpoint_interval == 0:
            try:
                self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.db.log("debug", f"Periodic WAL checkpoint completed (tick {self._tick_count})")
            except Exception as e:
                self.db.log("warn", f"Periodic WAL checkpoint failed: {e}")

    # -- 1. broker -----------------------------------------------------------
    async def broker_health(self) -> None:
        now = time.time()
        if self.broker.connected:
            if self.down_since is not None:
                mins = (now - self.down_since) / 60
                self.db.log("info", f"Broker connection is back (was down {mins:.0f} min)",
                            alert=self._down_alerted)
            self.down_since, self._down_alerted, self._backoff = None, False, 30.0
            return
        if self.down_since is None:
            self.down_since = now
            self._next_reconnect = now          # first retry right away
        if not self._down_alerted and now - self.down_since >= self.s.broker_down_alert_minutes * 60:
            err = self.broker.error or "no details"
            hint = (" This looks like a login problem: run ./auth.sh (macOS/Linux) or .\\auth.ps1 (Windows) and approve it in the "
                    "Webull app." if self.s.broker_mode == "webull" and AUTH_PATTERNS.search(err)
                    else "")
            self.db.log("error", f"Broker disconnected for {(now - self.down_since) / 60:.0f} min "
                                 f"and reconnects keep failing: {err}.{hint} Stops already at "
                                 "Webull stay active.", alert=True)
            self._down_alerted = True
        if self.engine.running or self.engine.flattening or now < self._next_reconnect:
            return
        try:
            await self.broker.start()
            self.portfolio.invalidate()
            self.db.log("info", f"Reconnected to MCP server ({len(self.broker.tools)} tools)")
        except BrokerError as e:
            self._next_reconnect = now + self._backoff
            log.info("reconnect failed (next try in %.0fs): %s", self._backoff, e)
            self._backoff = min(self._backoff * 2, 600.0)

    # -- 3. scheduler ----------------------------------------------------------
    def auto_start_scheduler(self) -> None:
        e = self.engine
        if (self.s.auto_start_scheduler and not e.kill_switch and not e.scheduler_on
                and not e.scheduler_stopped_by_user):
            e.start_scheduler()

    # -- 4. automatic kill switch ---------------------------------------------
    async def check_kill_triggers(self) -> str | None:
        """Trip the kill switch if a trigger fires. Returns the reason, or None."""
        e, s = self.engine, self.s
        if e.kill_switch:
            return None
        reason = None
        n = s.kill_max_consecutive_errors
        if n and max(e.run_failures, e.order_failures) >= n:
            what = "AI runs" if e.run_failures >= n else "orders"
            reason = (f"{max(e.run_failures, e.order_failures)} failed {what} in a row "
                      f"(last: {e.last_failure})")
        if reason is None and (s.kill_max_drawdown_pct > 0 or s.kill_daily_loss_usd > 0):
            try:
                bal = (await self.portfolio.snapshot(max_age=120))["balance"]
            except BrokerError:
                bal = {}
            reason = self._money_trigger(bal)
        if reason:
            await self.trip(reason)
        return reason

    def _money_trigger(self, bal: dict) -> str | None:
        s = self.s
        net, day = bal.get("net_liq"), bal.get("day_pnl")
        if net is not None and net > 0:
            peak = self.db.get_kv("equity_peak")
            if peak is None or net > peak:
                peak = net
                self.db.set_kv("equity_peak", peak)
            self.drawdown_pct = round((1 - net / peak) * 100, 2) if peak else None
            if s.kill_max_drawdown_pct > 0 and net <= peak * (1 - s.kill_max_drawdown_pct / 100):
                return (f"drawdown: net liquidation ${net:,.2f} is {self.drawdown_pct:.1f}% below "
                        f"its peak ${peak:,.2f} (limit {s.kill_max_drawdown_pct:g}%)")
        if s.kill_daily_loss_usd > 0 and day is not None and day <= -s.kill_daily_loss_usd:
            if self.db.get_kv("daily_loss_ack") != now_et().date().isoformat():
                return f"daily loss: day P&L ${day:,.2f} (limit -${s.kill_daily_loss_usd:,.2f})"
        return None

    async def daily_benchmark(self) -> None:
        """Record completed sessions for the performance report (read-only).

        The broker is queried at most every five minutes; the recorder itself is
        idempotent per day. Failures are logged and never stop the supervisor.
        """
        s = self.s
        if not getattr(s, "performance_daily_enabled", False):
            return
        now = time.time()
        if now - self._last_benchmark_check < 300:
            return
        self._last_benchmark_check = now
        if not self.broker.connected:
            return
        from .benchmark import record_daily_benchmark
        try:
            row = await record_daily_benchmark(self.db, self.broker, s)
            if row and row.get("created"):
                self._benchmark_day = row["day"]
        except Exception as e:  # noqa: BLE001 - measurement is not trading-critical
            log.warning("Daily benchmark failed: %s", e)

    async def portfolio_vs_spy(self) -> dict | None:
        """Account return vs SPY over the last SPY_BENCHMARK_WINDOW_DAYS calendar days.

        Compares the real equity curve (cash drag included) with SPY on the same dates.
        Returns None when there is not enough history yet."""
        from datetime import timedelta
        from .broker import bar_day
        from .market import now_utc
        s = self.s
        since = (now_utc() - timedelta(days=s.spy_benchmark_window_days)).isoformat(timespec="seconds")
        snaps = [r for r in self.db.equity(since) if r.get("net_liq")]
        if len(snaps) < 2:
            return None
        start_ts, end_ts = snaps[0]["ts"], snaps[-1]["ts"]
        # ignore windows much shorter than requested (e.g. just started)
        from datetime import datetime
        span = (datetime.fromisoformat(end_ts) - datetime.fromisoformat(start_ts)).total_seconds() / 86400
        if span < s.spy_benchmark_window_days * 0.8:
            return None
        bars = await self.broker.daily_bars("SPY", s.spy_benchmark_window_days + 15)
        d0, d1 = datetime.fromisoformat(start_ts).date(), datetime.fromisoformat(end_ts).date()
        before = [b for b in bars if bar_day(b["time"]) <= d0]
        after = [b for b in bars if bar_day(b["time"]) <= d1]
        if not before or not after:
            return None
        spy_ret = (after[-1]["close"] / before[-1]["close"] - 1) * 100
        acct_ret = (snaps[-1]["net_liq"] / snaps[0]["net_liq"] - 1) * 100
        # NOTE: deposits/withdrawals inside the window distort this; keep the account funded steadily.
        return {"account_pct": round(acct_ret, 3), "spy_pct": round(spy_ret, 3),
                "alpha_pct": round(acct_ret - spy_ret, 3), "days": round(span, 1)}

    async def check_spy_benchmark(self) -> str | None:
        """Is the ACCOUNT (not individual proposals) beating SPY? Returns the reason if not."""
        s = self.s
        if not s.spy_benchmark_enabled:
            return None
        if self.engine.kill_switch:
            return None  # already stopped; a trip must not repeat

        import time
        now = time.time()
        if (self._spy_benchmark_cache is not None and
                now - self._spy_benchmark_cache_time < self._spy_benchmark_cache_ttl):
            res = self._spy_benchmark_cache
        else:
            try:
                res = await self.portfolio_vs_spy()
            except Exception as e:  # noqa: BLE001
                log.warning(f"SPY benchmark check failed: {e}")
                return None
            self._spy_benchmark_cache, self._spy_benchmark_cache_time = res, now
        if not res:
            return None

        alpha = res["alpha_pct"]
        if alpha >= s.spy_benchmark_min_outperformance_pct:
            return None
        verb = "Trailing SPY" if alpha < 0 else "Beating SPY by less than the target"
        reason = (f"SPY benchmark: account {res['account_pct']:+.2f}% vs SPY {res['spy_pct']:+.2f}% over "
                  f"{res['days']:g}d (alpha {alpha:+.2f}%, need {s.spy_benchmark_min_outperformance_pct:+g}%). {verb}.")
        action = s.spy_benchmark_action
        if action == "warn":
            self.db.log("warn", reason, alert=True)
        elif action == "pause":
            await self.engine.set_kill_switch(True, f"automatic SPY benchmark: {reason}")
        elif action == "kill":
            await self.engine.set_kill_switch(True, f"automatic SPY benchmark: {reason}")
            try:
                await self.engine.flatten("auto", f"SPY benchmark: {reason}")
            except Exception as e:
                self.db.log("error", f"SPY benchmark flatten failed: {e}", alert=True)
        return reason

    async def trip(self, reason: str) -> None:
        e, action = self.engine, self.s.kill_action
        await e.set_kill_switch(True, f"automatic: {reason}")
        try:
            if action == "cancel_entries":
                await e.cancel_open_orders(False, "automatic kill switch")
            elif action == "flatten":
                await e.flatten("auto", reason)
        except (BrokerError, ValueError) as err:
            self.db.log("error", f"Kill action '{action}' could not finish: {err}", alert=True)

    def on_release(self) -> None:
        """You released the kill switch: start the drawdown peak fresh from the current
        value and stop the daily-loss trigger from firing again today."""
        self.db.set_kv("equity_peak", None)
        self.db.set_kv("daily_loss_ack", now_et().date().isoformat())
        self.drawdown_pct = None

    # -- 5. Webull login -------------------------------------------------------
    def check_token(self, force: bool = False) -> dict | None:
        if self.s.broker_mode != "webull":
            return None
        now = time.time()
        if (not force and now - self._last_token_check < 3600
                and (self.token or {}).get("status") != "missing"):
            return self.token
        self._last_token_check = now
        self.token = info = read_token_info(self.s.token_dir)
        if info["status"] == "missing" and self.broker.connected:
            # Some Webull environments (e.g. the sandbox) run without a login token, so no
            # conf/token.txt is ever written. If the broker is connected, no login is needed.
            self.token = info = {"status": "not_required", "expires_at": None,
                                 "days_left": None, "estimated": False}
            return info
        today = now_et().date().isoformat()
        if info["status"] == "missing":
            msg = ("No Webull login found (conf/token.txt). Run ./auth.sh (macOS/Linux) or .\\auth.ps1 (Windows) and approve it in "
                   "the Webull app.")
        elif info["status"] not in ("NORMAL", "unknown"):
            msg = (f"Webull login status is {info['status']}: run ./auth.sh (macOS/Linux) or .\\auth.ps1 (Windows) and approve it in "
                   "the Webull app.")
        elif info["days_left"] is not None and info["days_left"] <= self.s.token_warn_days:
            msg = (f"Webull login expires in {info['days_left']:.1f} day(s) "
                   f"({info['expires_at'][:16]} UTC). Run ./auth.sh (macOS/Linux) or .\\auth.ps1 (Windows) before then, or the app "
                   "stops trading.")
        else:
            return info
        if self.db.get_kv("token_warned") != today:
            self.db.set_kv("token_warned", today)
            self.db.log("warn", msg, alert=True)
        return info

    # -- 6. daily summary ------------------------------------------------------
    async def daily_summary(self) -> None:
        if not self.s.alert_daily_summary or not self.alerts.channels:
            return
        t = now_et()
        close = session_close(t.date())
        if close is None:
            return
        due = datetime.combine(t.date(), close, ET) + timedelta(minutes=SUMMARY_DELAY_MIN)
        today = t.date().isoformat()
        if t < due or self.db.get_kv("summary_date") == today:
            return
        self.db.set_kv("summary_date", today)
        self.alerts.notify(await self.summary_text(), level="info",
                           title="Trader daily summary", force=True)

        # Phase 7: Run nightly learning job after daily summary
        if self.learner is not None:
            await self.run_nightly_job()

    async def run_nightly_job(self) -> dict | None:
        """Run the Phase 7 nightly learning job (after market close)."""
        if self.learner is None:
            return None
        try:
            self.db.log("info", "Starting nightly learning job")
            result = await self.learner.run_nightly_job(self.engine)
            # Include nightly job summary in alerts if there were significant findings
            if result.get("review") or result.get("lessons_applied", 0) > 0 or result.get("ml_retrained"):
                summary_lines = ["Nightly learning job completed:"]
                if result.get("review"):
                    summary_lines.append(f"  Review: {result['review'].get('evidence_strength', 'unknown')} evidence")
                if result.get("lessons_applied", 0) > 0:
                    summary_lines.append(f"  Applied {result['lessons_applied']} lesson(s) to engine")
                if result.get("ml_retrained"):
                    ml_res = result.get("ml_result", {})
                    summary_lines.append(f"  ML retrained: model #{ml_res.get('id')} ({ml_res.get('status')})")
                if result.get("parameter_proposals", 0) > 0:
                    summary_lines.append(f"  Proposed {result['parameter_proposals']} parameter lesson(s)")
                if result.get("errors"):
                    summary_lines.append(f"  Errors: {len(result['errors'])}")
                self.alerts.notify("\n".join(summary_lines), level="info", title="Nightly Learning Job")
            return result
        except Exception as e:  # noqa: BLE001
            self.db.log("error", f"Nightly job failed: {type(e).__name__}: {e}")
            self.alerts.notify(f"Nightly learning job failed: {type(e).__name__}: {e}", level="error",
                               title="Nightly Job Error")
            return None

    async def summary_text(self) -> str:
        e = self.engine
        bal, positions = {}, []
        if self.broker.connected:
            try:
                snap = await self.portfolio.snapshot(max_age=60)
                bal, positions = snap["balance"], snap["positions"]
            except BrokerError:
                pass
        since = start_of_today_et_utc().isoformat(timespec="seconds")
        cost = sum(u["cost_usd"] or 0 for u in self.db.usage_since(since))
        placed = self.db.count_placed_today()

        def money(v):
            return "n/a" if v is None else f"${v:,.2f}"

        lines = [f"Net liquidation {money(bal.get('net_liq'))}, day P&L {money(bal.get('day_pnl'))}",
                 f"Orders placed today: {placed}. Open positions: {len(positions)}"
                 + (f" ({', '.join(p['symbol'] for p in positions)})" if positions else ""),
                 f"LLM cost today: ~${cost:.2f}",
                 f"Mode: {e.trading_mode}, scheduler {'on' if e.scheduler_on else 'off'}"]
        if e.kill_switch:
            lines.append(f"KILL SWITCH ON: {e.kill_reason}")
        if self.token and self.token.get("days_left") is not None:
            lines.append(f"Webull login: {self.token['days_left']:.1f} day(s) left")
        return "\n".join(lines)

    # -- dashboard -------------------------------------------------------------
    def status(self) -> dict:
        e = self.engine
        # SPY benchmark info (use cached data if available)
        spy_info = {}
        if self.s.spy_benchmark_enabled:
            try:
                if self._spy_status_cache is not None:
                    buys_vs_spy = self._spy_status_cache
                else:
                    from .learning import Learner
                    learner = Learner(self.s, self.broker, self.db)
                    stats = learner.stats()
                    buys_vs_spy = stats.get("buys_vs_spy", {})
                    import time
                    self._spy_status_cache = buys_vs_spy

                spy_info = {
                    "enabled": True,
                    "min_outperformance_pct": self.s.spy_benchmark_min_outperformance_pct,
                    "window_days": self.s.spy_benchmark_window_days,
                    "min_trades": self.s.spy_benchmark_min_trades,
                    "action": self.s.spy_benchmark_action,
                    "trades_count": buys_vs_spy.get("n", 0),
                    "avg_buy_pct": buys_vs_spy.get("avg_buy_5d"),
                    "avg_spy_pct": buys_vs_spy.get("avg_spy_5d"),
                    "avg_alpha_pct": buys_vs_spy.get("avg_alpha_5d"),
                    "alpha_hit_rate": buys_vs_spy.get("alpha_hit_rate"),
                    "passing": True,
                }
                if spy_info["trades_count"] >= self.s.spy_benchmark_min_trades:
                    spy_info["passing"] = (spy_info["avg_alpha_pct"] or 0) >= self.s.spy_benchmark_min_outperformance_pct
            except Exception:
                spy_info = {"enabled": True, "error": "Unable to compute"}
        return {
            "last_tick": self.last_tick, "error": self.last_error,
            "broker_down_since": datetime.fromtimestamp(self.down_since, timezone.utc)
            .isoformat(timespec="seconds") if self.down_since else None,
            "kill_reason": e.kill_reason, "kill_at": e.kill_at,
            "kill_action": self.s.kill_action,
            "run_failures": e.run_failures, "order_failures": e.order_failures,
            "max_errors": self.s.kill_max_consecutive_errors,
            "drawdown_pct": self.drawdown_pct, "max_drawdown_pct": self.s.kill_max_drawdown_pct,
            "equity_peak": self.db.get_kv("equity_peak"),
            "kill_daily_loss_usd": self.s.kill_daily_loss_usd,
            "unresolved_orders": len(self.db.unresolved_proposals()),
            "token": self.token, "auto_start": self.s.auto_start_scheduler,
            "scheduler_stopped_by_user": e.scheduler_stopped_by_user,
            "spy_benchmark": spy_info,
        }


def read_token_info(token_dir) -> dict:
    """Expiry and status of the Webull login from conf/token.txt. The token itself
    (line 1) is skipped and never read into memory."""
    path = token_dir / "token.txt"
    if not path.exists():
        return {"status": "missing", "expires_at": None, "days_left": None, "estimated": False}
    try:
        with open(path, encoding="utf-8") as f:
            f.readline()                      # the token: skip it
            raw_exp, status = f.readline().strip(), f.readline().strip()
    except OSError:
        raw_exp, status = "", ""
    exp = None
    if raw_exp.isdigit():
        n = int(raw_exp)
        exp = n / 1000 if n > 10**12 else n if n > 10**9 else None
    estimated = exp is None
    if estimated:   # unknown format: the token lasts about 15 days from its last refresh
        exp = path.stat().st_mtime + TOKEN_LIFETIME_DAYS * 86_400
    expires = datetime.fromtimestamp(exp, timezone.utc)
    return {"status": status or "unknown", "expires_at": expires.isoformat(timespec="minutes"),
            "days_left": round((exp - time.time()) / 86_400, 2), "estimated": estimated}
