"""OptionExitManager (QA H1): the only thing that ever closes a held option.

For every held long option the RiskLoop evaluates, on each pass:

  * premium stop        mark <= entry x (1 - OPTIONS_EXIT_STOP_PCT)
  * take profit         mark >= entry x (1 + OPTIONS_EXIT_TAKE_PROFIT_PCT)   (needs a fresh quote)
  * DTE exit            days to expiry <= OPTIONS_EXIT_DTE
  * expiry-day close    expiry day, at or after OPTIONS_EXPIRY_CLOSE_TIME (ET)

and on a trigger creates a ``SELL_TO_CLOSE`` proposal (``decided_by="risk_loop"``) and sends it
IMMEDIATELY as a marketable limit order (the bid; for a take profit the mid on the first attempt,
then the bid). Exits are never gated by ``trading_mode`` (approve/auto), by the kill switch, by
the options-enabled flag, or by the broker guard (a ``*_TO_CLOSE`` order is a closing order and
is sent with ``allow_when_killed=True``). A stale or missing quote never refuses an exit: the
order falls back to a price derived from the last/position mark, or to the 0.01 floor.

There is deliberately NO resting broker-side protective order for options: Webull's option STOP
orders on thin quotes can trigger on a wide quote and fill far below the market, and not every
transport supports them. The RiskLoop (this manager, evaluated every ``risk_loop_seconds``) is
the protection, so it must be running whenever an option is held (the engine starts it with
``start_risk_loop()``; ``startup_reconcile`` arms it).

One exit per contract is in flight at a time: the DB's open-proposal index (symbol + side) and
the in-flight check below dedupe; an exit order that is still unfilled after ``REPRICE_SECONDS``
is cancelled and re-sent at the current bid.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

from ..broker import BrokerError
from ..db import DuplicateProposalError
from ..market import is_trading_day, market_is_open, now_et, now_utc
from ..quotes import Quote
from .util import parse_occ

log = logging.getLogger("app.engine")

_OCC_FULL = re.compile(r"^[A-Z]{1,5}\d{6}[CP]\d{8}$")
TRIGGER_PRIORITY = ("expiry_close", "dte", "underlying_stop", "underlying_time", "stop",
                    "take_profit")


@dataclass
class HeldOption:
    symbol: str
    qty: float
    cost: float | None = None        # per-share premium, as reported by the broker
    mark: float | None = None        # broker's last/mark for the position

    @property
    def parsed(self) -> dict | None:
        return parse_occ(self.symbol)


def _num(raw) -> float | None:
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _occ_from_leg(line: str) -> str | None:
    """OCC symbol from a Webull US leg line: ``Leg: TLT  Side: LONG  Qty: 3  Type: OPTION
    Option: CALL  Strike: 82  Exp: 2026-12-31``. None when any part is missing."""
    und = re.search(r"\bLeg:\s*([A-Za-z.]+)", line)
    kind = re.search(r"\bOption:\s*(CALL|PUT|C|P)\b", line, re.I)
    strike = re.search(r"\bStrike:\s*([\d.,]+)", line)
    exp = re.search(r"\bExp:\s*(\d{4})-?(\d{2})-?(\d{2})", line)
    if not (und and kind and strike and exp):
        return None
    k = _num(strike.group(1))
    if k is None:
        return None
    return (f"{und.group(1).upper().replace('.', '')}{exp.group(1)[2:]}{exp.group(2)}{exp.group(3)}"
            f"{kind.group(1).upper()[0]}{round(k * 1000):08d}")


def parse_held_options(text: str) -> list[HeldOption]:
    """Held option rows from the broker's positions text. Two layouts:
    - one row per line with an OCC ``Symbol:`` (the mock);
    - Webull US: a position line ``Symbol: <underlying> ... Type: OPTION ... Cost: .. Last: ..``
      followed by indented ``Leg:`` lines carrying side/option/strike/expiry. Only single-leg
      positions are read; a leg is never paired with any other position's line.
    Stock rows are never returned."""
    out: dict[str, HeldOption] = {}

    def add(name, qty_v, line):
        cost = re.search(r"\b(?:Cost|Avg Cost|Average Cost)\s*[:=]\s*(-?[\d.,]+)", line, re.I)
        mark = re.search(r"\b(?:Mark|Last)\s*[:=]\s*(-?[\d.,]+)", line, re.I)
        row = out.setdefault(name, HeldOption(name, 0.0))
        row.qty += qty_v
        if cost and _num(cost.group(1)):
            row.cost = _num(cost.group(1))
        if mark and _num(mark.group(1)):
            row.mark = _num(mark.group(1))

    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        sym = re.search(r"\bSymbol:\s*(\S+)", line)
        qty = re.search(r"(?<!Available )\bQty:\s*(-?[\d.,]+)", line)
        if sym and qty and _OCC_FULL.match(sym.group(1).upper()):
            add(sym.group(1).upper(), _num(qty.group(1)) or 0.0, line)
        elif sym and re.search(r"\bType:\s*OPTION\b", line, re.I):
            legs = []
            j = i + 1
            while j < len(lines) and re.match(r"\s*Leg:", lines[j]):
                legs.append(lines[j])
                j += 1
            if len(legs) == 1:
                occ = _occ_from_leg(legs[0])
                q = _num(qty.group(1)) if qty else None
                if q is None:
                    lq = re.search(r"\bQty:\s*(-?[\d.,]+)", legs[0])
                    q = _num(lq.group(1)) if lq else None
                if occ and q:
                    if q > 0 and re.search(r"\bSide:\s*(SHORT|SELL)", legs[0], re.I):
                        q = -q
                    add(occ, q, line)
            i = j
            continue
        i += 1
    return list(out.values())


def parse_close_time(raw: str) -> dtime:
    try:
        hh, mm = str(raw).strip().split(":")[:2]
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return dtime(15, 45)


def evaluate_exit(settings, *, entry: float | None, mark: float | None, expiration: str | None,
                  now: datetime, fresh_mark: bool = True, underlying_px: float | None = None,
                  underlying_stop: float | None = None,
                  held_trading_days: int | None = None) -> tuple[str, str] | None:
    """The first exit trigger that fires, as ``(trigger, detail)``, else None. Pure.

    ``now`` is a timezone-aware ET datetime. ``fresh_mark`` False means the mark comes from a
    stale or missing quote: protective triggers (expiry, DTE, stop) still fire, a take profit
    does not. ``underlying_px`` / ``underlying_stop`` (the stock's price and the stop saved at
    entry) and ``held_trading_days`` drive the auto-entry exits; each is skipped when None.
    """
    s = settings
    fired: dict[str, str] = {}
    if (getattr(s, "options_exit_underlying_stop", True) and underlying_px and underlying_stop
            and underlying_px > 0 and underlying_stop > 0
            and underlying_px <= underlying_stop + 1e-9):
        fired["underlying_stop"] = (f"underlying {underlying_px:.2f} <= its stop "
                                    f"{underlying_stop:.2f}")
    max_hold = int(getattr(s, "exit_max_hold_days", 0) or 0)
    if max_hold > 0 and held_trading_days is not None and held_trading_days >= max_hold:
        fired["underlying_time"] = (f"held {held_trading_days} trading days >= "
                                    f"time stop {max_hold}")
    if expiration:
        try:
            dte = (date.fromisoformat(expiration) - now.date()).days
        except ValueError:
            dte = None
        if dte is not None:
            close_at = parse_close_time(s.options_expiry_close_time)
            if dte < 0 or (dte == 0 and now.time() >= close_at):
                fired["expiry_close"] = (f"expiry day close-out at/after {close_at:%H:%M} ET"
                                         if dte == 0 else f"expired {-dte} day(s) ago")
            if s.options_exit_dte >= 0 and dte <= s.options_exit_dte:
                fired["dte"] = f"{dte} DTE <= exit threshold {s.options_exit_dte}"
    if entry and entry > 0 and mark and mark > 0:
        if s.options_exit_stop_pct > 0:
            floor = entry * (1 - s.options_exit_stop_pct)
            if mark <= floor + 1e-9:
                fired["stop"] = (f"mark {mark:.2f} <= stop {floor:.2f} "
                                 f"(entry {entry:.2f} -{s.options_exit_stop_pct:.0%})")
        if s.options_exit_take_profit_pct > 0 and fresh_mark:
            target = entry * (1 + s.options_exit_take_profit_pct)
            if mark >= target - 1e-9:
                fired["take_profit"] = (f"mark {mark:.2f} >= target {target:.2f} "
                                        f"(entry {entry:.2f} +{s.options_exit_take_profit_pct:.0%})")
    for name in TRIGGER_PRIORITY:
        if name in fired:
            return name, fired[name]
    return None


class OptionExitManager:
    REPRICE_SECONDS = 30.0          # an unfilled exit order older than this is re-sent at the bid
    RETRY_AFTER_FAIL_SECONDS = 10.0

    def __init__(self, engine) -> None:
        self.eng = engine
        self.s = engine.s
        self.db = engine.db
        self.broker = engine.broker
        self.held: dict[str, HeldOption] = {}
        self.last_run: float | None = None
        self.last_error: str | None = None
        self._last_attempt: dict[str, float] = {}
        self._warned: set[str] = set()
        self.exits_sent = 0

    # -- reading what is held --------------------------------------------------
    async def held_options(self) -> list[HeldOption]:
        text = await self.broker.positions_text()
        # an empty/"No data" reply must not be read as "every option closed" by outcome recording
        self._positions_ok = bool(text and text.strip() and not text.lstrip().startswith("No data"))
        rows = parse_held_options(text)
        if not rows and "Leg:" not in (text or ""):   # other block formats: the broker's own parser
            try:
                for r in await self.broker.option_positions():
                    if r.get("symbol") and r.get("qty"):
                        rows.append(HeldOption(r["symbol"], float(r["qty"]),
                                               r.get("cost_basis"), r.get("mark_price")))
            except Exception as exc:  # noqa: BLE001
                log.warning("held-option row parse failed: %s", exc)
                pass
        # options a multi-strategy sleeve holds (e.g. the put-write sleeve's short puts) are
        # managed by that sleeve, never by this manager
        try:
            hidden = self.eng.db.sleeve_option_symbols()
        except Exception as exc:  # noqa: BLE001 - stub DBs
            log.debug("sleeve_option_symbols unavailable: %s", exc)
            hidden = set()
        return [r for r in rows if r.symbol.upper() not in hidden] if hidden else rows

    def entry_context(self, pos: HeldOption) -> tuple[float | None, str | None]:
        """(underlying stop, entry timestamp) saved on the placed BUY_TO_OPEN proposal."""
        try:
            for r in self.db.proposals(100, pos.symbol):
                if (r.get("side") == "BUY" and str(r.get("position_intent") or "").upper() ==
                        "BUY_TO_OPEN" and r.get("status") in ("placed", "unknown")):
                    sl = r.get("sl")
                    return (float(sl) if sl else None), (r.get("placed_at") or r.get("created_at"))
        except Exception as exc:  # noqa: BLE001
            log.warning("option entry-order lookup failed: %s", exc)
            pass
        return None, None

    @staticmethod
    def trading_days_held(entry_ts: str | None, now: datetime) -> int | None:
        """Trading days from the entry date to ``now`` (None when the entry is unknown)."""
        if not entry_ts:
            return None
        try:
            start = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
        except ValueError:
            return None
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        d, end, n = start.astimezone(now.tzinfo).date(), now.date(), 0
        while d < end:
            d += timedelta(days=1)
            if is_trading_day(d):
                n += 1
        return n

    def entry_price(self, pos: HeldOption) -> float | None:
        """Premium paid per contract (per share): the latest placed BUY_TO_OPEN proposal's limit
        price (our own number, in per-share units), else the broker's cost for the position
        (assumed per-share, as the mock and Webull's average-cost field report it), else the
        value remembered at arming time."""
        entry = None
        try:
            for r in self.db.proposals(100, pos.symbol):
                if (r.get("side") == "BUY" and str(r.get("position_intent") or "").upper() ==
                        "BUY_TO_OPEN" and r.get("status") in ("placed", "unknown")
                        and (r.get("limit_price") or 0) > 0):
                    entry = float(r["limit_price"])
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("option entry price lookup failed: %s", exc)
            entry = None
        if entry is None and pos.cost and pos.cost > 0:
            entry = float(pos.cost)
        if entry is None:
            cached = self.db.get_kv(f"option_entry:{pos.symbol}")
            if cached:
                entry = float(cached)
        elif self.db.get_kv(f"option_entry:{pos.symbol}") != entry:
            self.db.set_kv(f"option_entry:{pos.symbol}", entry)
        return entry

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """One batched snapshot request for all held contracts."""
        out: dict[str, Quote] = {}
        if not symbols:
            return out
        for i in range(0, len(symbols), 20):
            chunk = symbols[i:i + 20]
            snaps = await self.broker.parsed_option_snapshots(chunk)
            for sym in chunk:
                d = snaps.get(sym)
                if d:
                    out[sym] = Quote(symbol=sym, bid=d.get("bid"), ask=d.get("ask"),
                                     last=d.get("last"), as_of=float(d.get("as_of") or 0.0),
                                     iv=d.get("iv"), delta=d.get("delta"),
                                     volume=d.get("volume"), open_interest=d.get("open_interest"))
        return out

    # -- arming (startup) ------------------------------------------------------
    async def arm(self) -> list[HeldOption]:
        """Read the held options, remember their entry prices and say what is being watched."""
        rows = [r for r in await self.held_options() if r.qty]
        self.held = {r.symbol: r for r in rows}
        for r in rows:
            entry = self.entry_price(r)
            self.db.log("info", f"Option exit manager armed: {r.qty:g} x {r.symbol}, entry "
                                f"{entry if entry else 'unknown'} (stop -{self.s.options_exit_stop_pct:.0%}, "
                                f"target +{self.s.options_exit_take_profit_pct:.0%}, "
                                f"exit at <= {self.s.options_exit_dte} DTE, expiry close "
                                f"{self.s.options_expiry_close_time} ET)")
        return rows

    # -- one pass --------------------------------------------------------------
    async def run_once(self) -> list[dict]:
        """Evaluate every held option once; send the exits that fired. Returns the actions."""
        actions: list[dict] = []
        self.last_run = time.time()
        rows = [r for r in await self.held_options() if r.qty]
        self.held = {r.symbol: r for r in rows}
        try:        # leave a training record for every auto option trade that has closed
            from .. import options_ml
            options_ml.record_closed_outcomes(self.db, set(self.held), now_et().date(),
                                              positions_ok=getattr(self, "_positions_ok", True))
        except Exception as e:  # noqa: BLE001 - recording must never disturb exits
            log.debug("option outcome recording skipped: %r", e)
        if not rows:
            return actions
        if not market_is_open():
            return actions          # option orders only work in the regular session
        try:
            quotes = await self.quotes([r.symbol for r in rows if r.qty > 0])
        except BrokerError as e:
            self.last_error = f"option quotes: {e}"
            quotes = {}             # stale/missing quotes never block an exit (see evaluate_exit)
        now = now_et()
        for pos in rows:
            try:
                act = await self._handle(pos, quotes.get(pos.symbol), now)
            except BrokerError as e:
                self.last_error = f"{pos.symbol}: {e}"
                log.warning("option exit for %s failed: %s", pos.symbol, e)
                continue
            if act:
                actions.append(act)
        return actions

    async def _handle(self, pos: HeldOption, q: Quote | None, now: datetime) -> dict | None:
        if pos.qty < 0:
            if pos.symbol not in self._warned:
                self._warned.add(pos.symbol)
                self.db.log("error", f"SHORT option position held: {pos.qty:g} x {pos.symbol}. "
                                     "This app never opens one; close it manually.", alert=True)
            return None
        occ = pos.parsed
        if not occ or int(pos.qty) < 1:
            return None
        entry = self.entry_price(pos)
        max_age = float(self.s.options_quote_max_age_seconds)
        fresh = bool(q and q.is_fresh(max_age))
        mark = None
        if q is not None:
            mark = q.mid if q.two_sided else (q.last if q.last and q.last > 0 else None)
        if mark is None:
            mark = pos.mark
            fresh = False
        u_stop, entry_ts = self.entry_context(pos)
        u_px = None
        if u_stop and self.s.options_exit_underlying_stop:
            try:
                u_px = await self.broker.last_price(occ["underlying"])
            except BrokerError:
                u_px = None     # a missing stock price never blocks the other triggers
        hit = evaluate_exit(self.s, entry=entry, mark=mark, expiration=occ["expiration"],
                            now=now, fresh_mark=fresh, underlying_px=u_px, underlying_stop=u_stop,
                            held_trading_days=(self.trading_days_held(entry_ts, now)
                                               if u_stop else None))
        if hit is None:
            return None
        trigger, detail = hit
        return await self.exit_position(pos, trigger, detail, q, entry, actor="risk_loop")

    # -- sending ---------------------------------------------------------------
    def _limit_price(self, pos: HeldOption, q: Quote | None, trigger: str, attempt: int) -> float:
        bid = q.bid if q is not None and q.bid and q.bid > 0 else None
        if bid is not None:
            if trigger == "take_profit" and attempt == 0 and q.two_sided:
                return round(q.mid, 2)          # patient first try, then step down to the bid
            return round(bid, 2)
        ref = None
        if q is not None and q.last and q.last > 0:
            ref = q.last
        elif pos.mark and pos.mark > 0:
            ref = pos.mark
        if ref is not None:
            return max(0.01, round(ref * 0.90, 2))   # marketable: well below the last print
        return 0.01

    def _in_flight(self, symbol: str) -> tuple[str, dict | None, int]:
        """('none'|'wait'|'stale', row, attempts). 'wait': an exit is in flight and young.
        'stale': a placed exit is older than REPRICE_SECONDS and should be re-sent."""
        rows = [r for r in self.db.proposals(30, symbol)
                if r.get("side") == "SELL"
                and str(r.get("position_intent") or "").upper() == "SELL_TO_CLOSE"]
        attempts = len([r for r in rows if r.get("status") in ("placed", "unknown", "failed", "submitting")
                        and self._age_seconds(r) < 3600])
        for r in rows:
            st = r.get("status")
            if st in ("pending", "submitting"):
                return "wait", r, attempts
            if st in ("placed", "unknown"):
                age = self._age_seconds(r)
                if age < self.REPRICE_SECONDS:
                    return "wait", r, attempts
                if age < 3600:
                    return "stale", r, attempts
        return "none", None, attempts

    @staticmethod
    def _age_seconds(row: dict) -> float:
        stamp = row.get("placed_at") or row.get("created_at")
        try:
            return (now_utc() - datetime.fromisoformat(stamp)).total_seconds()
        except (TypeError, ValueError):
            return 1e9

    async def exit_position(self, pos: HeldOption, trigger: str, detail: str, q: Quote | None,
                            entry: float | None, *, actor: str = "risk_loop") -> dict | None:
        """Create the SELL_TO_CLOSE proposal and send it now. Returns an action dict or None
        when an exit for this contract is already in flight."""
        eng, sym = self.eng, pos.symbol
        occ = pos.parsed
        qty = int(pos.qty)
        now_m = time.monotonic()
        if now_m - self._last_attempt.get(sym, -1e9) < self.RETRY_AFTER_FAIL_SECONDS:
            return None
        state, prior, attempts = self._in_flight(sym)
        if state == "wait":
            return None
        if state == "stale" and prior is not None:
            if not await self._cancel_stale(prior, sym):
                return None
            # the cancel may have raced a fill: size the new order from the live position
            fresh = {r.symbol: r for r in await self.held_options()}.get(sym)
            if fresh is None or int(fresh.qty) < 1:
                return None
            qty = int(fresh.qty)
        self._last_attempt[sym] = now_m
        limit = self._limit_price(pos, q, trigger, attempts)
        p = {"symbol": sym, "side": "SELL", "quantity": qty, "limit_price": limit,
             "confidence": None,
             "rationale": f"Option exit ({trigger}): {detail}"}
        extra = dict(asset_class="option", option_type=occ["option_type"],
                     strike_price=occ["strike"], expiration_date=occ["expiration"],
                     position_intent="SELL_TO_CLOSE",
                     instrument_json=json.dumps({"option_type": occ["option_type"],
                                                 "strike_price": occ["strike"],
                                                 "expiration_date": occ["expiration"],
                                                 "position_intent": "SELL_TO_CLOSE",
                                                 "multiplier": 100}),
                     strategy_type="SINGLE", decided_by=actor,
                     signal_id=None, sl=None, tp=None, ml_p=None,
                     consensus=[], risk_checks=[])
        async with eng._proposal_lock:
            try:
                pid = self.db.add_proposal(None, p, "pending", f"option exit: {trigger}", limit,
                                           None, **extra)
            except DuplicateProposalError:
                # an older pending exit row holds the slot: this exit supersedes it
                pid = None
                for row in self.db.open_proposals(sym, "SELL"):
                    if row["status"] == "pending":
                        self.db.update_proposal(row["id"], status="dismissed",
                                                reason="superseded by a risk-loop exit")
                    else:
                        return None      # submitting: an exit is already in flight
                try:
                    pid = self.db.add_proposal(None, p, "pending", f"option exit: {trigger}",
                                               limit, None, **extra)
                except DuplicateProposalError:
                    return None
        what = f"#{pid} SELL_TO_CLOSE {qty} {sym} @ {limit}"
        self.db.log("warn", f"OPTION EXIT [{trigger}] {what}: {detail}", alert=True)
        row = self.db.get_proposal(pid)
        ok, reasons = eng.option_risk.check_single_leg(row)
        if not ok:      # only a malformed order can fail here; still never blocks silently
            self.db.update_proposal(pid, status="rejected", reason="; ".join(reasons),
                                    decided_by=actor)
            self.db.log("error", f"Option exit {what} is malformed: {'; '.join(reasons)}",
                        alert=True)
            return {"symbol": sym, "trigger": trigger, "status": "rejected", "proposal_id": pid,
                    "quantity": qty, "limit_price": limit}
        if not self.db.claim_pending(pid):
            return None
        try:
            async with self.broker.flatten_mode():
                await eng._send(pid, row, actor, what)
        except Exception as e:  # noqa: BLE001 - never leave the row in "submitting"
            log.exception("option exit %s failed unexpectedly", what)
            cur = self.db.get_proposal(pid)
            if cur and cur["status"] == "submitting":
                self.db.update_proposal(pid, status="unknown", decided_by=actor,
                                        reason=f"unexpected error while sending: {e}")
        final = self.db.get_proposal(pid) or {}
        self.exits_sent += 1
        if eng.on_order:
            eng.on_order()
        return {"symbol": sym, "trigger": trigger, "status": final.get("status"),
                "proposal_id": pid, "quantity": qty, "limit_price": limit, "detail": detail}

    async def _cancel_stale(self, prior: dict, sym: str) -> bool:
        """Cancel a placed exit that did not fill so it can be re-sent at the bid. False when the
        cancel got no reply (the order may still be live: do not send a second one)."""
        coid = prior.get("client_order_id")
        if not coid:
            return True
        try:
            await self.broker.cancel_order(coid)
        except BrokerError as e:
            if e.transport:
                self.db.log("error", f"Cancel of the stale {sym} exit got no reply: {e}")
                return False
            # refused: the order already filled or is gone; the position read decides
        self.db.update_proposal(prior["id"], status="failed",
                                reason="exit not filled in time; cancelled and re-sent at the bid")
        return True

    async def close_all(self, why: str, *, actor: str = "flatten") -> list[dict]:
        """Close every held option now (used by flatten). Returns one dict per contract."""
        out = []
        rows = [r for r in await self.held_options() if r.qty > 0]
        quotes = {}
        try:
            quotes = await self.quotes([r.symbol for r in rows])
        except BrokerError:
            pass
        for pos in rows:
            act = await self.exit_position(pos, "flatten", why, quotes.get(pos.symbol),
                                           self.entry_price(pos), actor=actor)
            if act:
                out.append(act)
        return out
