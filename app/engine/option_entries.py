"""Options auto-entry (HANDOFF section 0 item 9): buy a long CALL alongside the stock entry.

Runs right after the equity BUY loop of the deterministic cycle, only when OPTIONS_ENABLED and
OPTIONS_AUTO_ENTRY are both on. For each "long" signal whose symbol is an allowed underlying it
picks one contract (expiry nearest the target DTE, strike nearest the target delta, liquid enough),
sizes whole contracts to the premium budget and submits the proposal through ``_on_proposal``,
so the option risk checks, the contract gate, the kill switch and the trading mode all still apply.
Exits are in option_exits.py (premium stop, DTE, expiry, the stock's stop and a time stop).
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta
from statistics import NormalDist

from .. import options_ml, ta
from ..broker import BrokerError
from ..eligibility import EligibilityChecker
from ..market import CLOSE, OPEN, now_et, order_session, session_close
from ..risk import working_buy_symbols
from .options_flow import iv_rank, normalize_iv
from .util import _is_option_symbol, parse_occ

log = logging.getLogger("app.engine")

_OCC_TOKEN = re.compile(r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b")
MAX_CANDIDATES_PER_RUN = 5
STRIKES_TO_QUOTE = 8


def option_tick(price: float) -> float:
    """Minimum price increment: 0.01 under $3, 0.05 at or above."""
    return 0.01 if price < 3.0 else 0.05


def entry_limit_price(bid: float, ask: float) -> float:
    """mid + half the way to the ask, rounded to the tick (never above the ask)."""
    mid = (bid + ask) / 2
    raw = mid + 0.5 * (ask - mid)
    tick = option_tick(raw)
    px = round(round(raw / tick) * tick, 2)
    return round(min(px, ask), 2)


def minutes_outside_edges(now: datetime) -> tuple[float, float]:
    """(minutes since the open, minutes until the close) of the regular session, ET."""
    t = now.astimezone(now.tzinfo)
    close_t = session_close(t.date()) or CLOSE
    since_open = (datetime.combine(t.date(), t.time()) - datetime.combine(t.date(), OPEN)
                  ).total_seconds() / 60
    to_close = (datetime.combine(t.date(), close_t) - datetime.combine(t.date(), t.time())
                ).total_seconds() / 60
    return since_open, to_close


class OptionEntriesMixin:
    # -- logging helper: one plain line per (underlying, reason) per run ----------------
    def _entry_skip(self, run_id: int, key: tuple, line: str) -> None:
        seen = self.__dict__.setdefault("_opt_entry_skips", {})
        bucket = seen.setdefault(run_id, set())
        if len(seen) > 20:                    # keep only recent runs
            for old in sorted(seen)[:-5]:
                seen.pop(old, None)
        if key in bucket:
            return
        bucket.add(key)
        self.db.log("info", line)

    async def propose_option_entries(self, run_id: int, sigs: list[dict]) -> int:
        """Propose one long call per qualifying signal. Returns how many were accepted."""
        s = self.s
        if not (s.options_enabled and s.options_auto_entry):
            return 0
        allowed = {u.upper() for u in s.options_allowed_underlyings}
        cands = [x for x in sigs if x.get("consensus") == "long" and x["symbol"] in allowed]
        if not cands:
            return 0
        now = now_et()
        # --- global skips (one line per run) ---
        if self.kill_switch:
            self._entry_skip(run_id, ("kill",), "Option entries skipped: the kill switch is on")
            return 0
        if order_session() != "CORE":
            self._entry_skip(run_id, ("session",),
                             "Option entries skipped: options only trade in the regular session")
            return 0
        n = s.options_entry_skip_open_close_minutes
        since_open, to_close = minutes_outside_edges(now)
        if n > 0 and (since_open < n or to_close < n):
            self._entry_skip(run_id, ("edge",),
                             f"Option entries skipped: within {n} minutes of the open or close")
            return 0

        # --- what is already held / pending on options ---
        try:
            held = [r for r in await self.option_exits.held_options() if r.qty and r.qty > 0]
        except Exception as e:  # noqa: BLE001 - cannot see positions: do not add risk
            self._entry_skip(run_id, ("positions",),
                             f"Option entries skipped: could not read option positions ({e})")
            return 0
        busy: dict[str, str] = {}
        for r in held:
            occ = r.parsed
            if occ:
                busy[occ["underlying"]] = "an option position is already open"
        pending_buys = 0
        for pr in self.db.open_proposals():
            if _is_option_symbol(pr["symbol"]) and pr["side"] == "BUY":
                pending_buys += 1
                occ = parse_occ(pr["symbol"])
                if occ:
                    busy[occ["underlying"]] = "an option order is already pending"
        for sym in working_buy_symbols(self.db):
            occ = parse_occ(sym)
            if occ:
                busy.setdefault(occ["underlying"], "an option order is already working")
        open_count = len({r.symbol for r in held}) + pending_buys
        placed_today = self.db.count_placed_today("option") + pending_buys

        ml_on = bool(s.options_ml_enabled)
        if ml_on:      # rank by P(win) at the configured target (stable: unscored keep their order)
            def _pre(x):
                p0 = options_ml.predict(options_ml.signal_features(x),
                                        s.options_entry_target_delta, s.options_entry_target_dte, None)
                return -1.0 if p0 is None else p0
            cands = sorted(cands, key=lambda x: -_pre(x))

        accepted = 0
        tried = 0
        for sig in cands:
            sym = sig["symbol"]
            if tried >= MAX_CANDIDATES_PER_RUN:
                break
            if sym in busy:
                self._entry_skip(run_id, (sym, "busy"),
                                 f"{sym} call skipped: {busy[sym]} on {sym}")
                continue
            if s.options_entry_max_open > 0 and open_count >= s.options_entry_max_open:
                self._entry_skip(run_id, (sym, "max_open"),
                                 f"{sym} call skipped: {open_count} option positions open "
                                 f"(limit {s.options_entry_max_open})")
                continue
            if s.options_entry_max_per_day > 0 and placed_today >= s.options_entry_max_per_day:
                self._entry_skip(run_id, (sym, "max_day"),
                                 f"{sym} call skipped: {placed_today} option orders today "
                                 f"(limit {s.options_entry_max_per_day})")
                continue
            if s.options_entry_earnings_days > 0:
                chk = EligibilityChecker(s, self.broker, self.signals, self.db)
                er = await chk._check_earnings(sym, s.options_entry_earnings_days)
                if not er.ok:
                    self._entry_skip(run_id, (sym, "earnings"),
                                     f"{sym} call skipped: {er.reason}")
                    continue
            tried += 1
            try:
                arm = (options_ml.explore_arm(run_id, sym, now.date(), s) if ml_on else None)
                p = await self._select_call(run_id, sig, now, arm=arm)
            except BrokerError as e:
                self._entry_skip(run_id, (sym, "broker"), f"{sym} call skipped: broker error ({e})")
                continue
            if p is None:
                continue
            res = await self._on_proposal(run_id, p)
            if res.startswith("ACCEPTED"):
                accepted += 1
                open_count += 1
                placed_today += 1
                busy[sym] = "an option order is already pending"
                self.db.log("info", f"Auto option entry for {sym}: {res}")
            else:
                self._entry_skip(run_id, (sym, "rejected"), f"{sym} call not placed: {res}")
        return accepted

    def _target_expiry(self, chain: dict, dte: int | None = None) -> str:
        t = dte if dte is not None else self.s.options_entry_target_dte
        return min(chain, key=lambda e: (abs(chain[e][0]["dte"] - t), e))

    def _near_target_strikes(self, rows: list[dict], spot: float,
                             delta: float | None = None) -> list[dict]:
        """~8 strikes nearest the target delta (assuming ~30% IV, only to choose what to quote)."""
        s = self.s
        dte = rows[0]["dte"]
        z = NormalDist().inv_cdf(min(0.99, max(0.01, delta if delta is not None
                                                  else s.options_entry_target_delta)))
        est = spot * math.exp(-z * 0.30 * math.sqrt(max(dte, 1) / 365))
        return sorted(rows, key=lambda c: (abs(c["strike"] - est), c["strike"]))[:STRIKES_TO_QUOTE]

    def pick_target_and_atm(self, chain: dict, spot: float) -> list[tuple[str, dict]]:
        """For the spread logger: the strike nearest the target delta and the nearest-ATM call,
        both in the target-DTE expiry."""
        rows = chain[self._target_expiry(chain)]
        tgt = self._near_target_strikes(rows, spot)[0]
        atm = min(rows, key=lambda c: (abs(c["strike"] - spot), c["strike"]))
        return [("target", tgt)] + ([("atm", atm)] if atm["symbol"] != tgt["symbol"] else [])

    async def _select_call(self, run_id: int, sig: dict, now: datetime,
                           arm: tuple[float, int] | None = None) -> dict | None:
        """Pick the contract and build the proposal dict, or None (the reason is logged)."""
        s, sym = self.s, sig["symbol"]
        skip = lambda why, tag: (self._entry_skip(run_id, (sym, tag), f"{sym} call skipped: {why}")  # noqa: E731
                                 or None)
        spot = await self.broker.last_price(sym)
        if not spot or spot <= 0:
            return skip("no stock price", "price")
        text = await self.broker.option_contracts([sym])
        today = now.date()
        chain: dict[str, list[dict]] = {}
        for tok in dict.fromkeys(_OCC_TOKEN.findall(text or "")):
            occ = parse_occ(tok)
            if not occ or occ["underlying"] != sym or occ["option_type"] != "CALL":
                continue
            dte = (date.fromisoformat(occ["expiration"]) - today).days
            if s.options_min_dte <= dte <= s.options_max_dte:
                chain.setdefault(occ["expiration"], []).append({**occ, "symbol": tok, "dte": dte})
        if not chain:
            return skip(f"no call expiring in {s.options_min_dte}-{s.options_max_dte} days", "chain")
        t_delta = arm[0] if arm else s.options_entry_target_delta
        expiry = self._target_expiry(chain, arm[1] if arm else None)
        rows = chain[expiry]
        near = self._near_target_strikes(rows, spot, t_delta)
        snaps = await self.broker.parsed_option_snapshots([c["symbol"] for c in near])

        best, reasons = None, []
        for c in near:
            d = snaps.get(c["symbol"]) or {}
            bid, ask, delta = d.get("bid"), d.get("ask"), d.get("delta")
            if not bid or not ask or bid <= 0 or ask < bid or delta is None:
                reasons.append("no two-sided quote")
                continue
            spread = (ask - bid) / ((ask + bid) / 2) * 100
            if spread > s.options_max_bid_ask_pct:
                reasons.append(f"spread {spread:.1f}%")
                continue
            if (d.get("volume") or 0) < s.options_min_volume:
                reasons.append("low volume")
                continue
            if (d.get("open_interest") or 0) < s.options_min_open_interest:
                reasons.append("low open interest")
                continue
            miss = abs(abs(delta) - t_delta)
            if best is None or miss < best[0]:
                best = (miss, c, d, spread)
        if best is None:
            why = ", ".join(sorted(set(reasons))) or "no contracts"
            return skip(f"no {expiry} call passed the liquidity checks ({why})", "liquidity")
        _, c, d, spread = best
        bid, ask = float(d["bid"]), float(d["ask"])
        # Size to the tightest cap that applies, so the order can never be refused downstream:
        # the entry budget, the option notional cap, and the broker guard's per-order caps
        # (app/broker/guard.py applies MAX_ORDER_USD / MAX_ORDER_NOTIONAL to options too). A
        # guard rejection counts toward the consecutive-failure kill switch, which halts stocks.
        caps = [c for c in (s.options_entry_max_premium_usd, s.options_max_notional_usd,
                            getattr(s, "max_order_usd", 0), getattr(s, "max_order_notional", 0))
                if c and c > 0]
        budget = min(caps) if caps else s.options_entry_max_premium_usd
        qty = int(budget // (ask * 100))
        if qty < 1:
            return skip(f"one contract costs ${ask * 100:,.0f}, budget ${budget:,.0f}", "budget")
        limit = entry_limit_price(bid, ask)
        iv = normalize_iv(d.get("iv"))
        rank = None
        try:
            series = self.db.atm_iv_series(sym, 252)
            if iv is not None and len(series) >= int(s.options_iv_rank_min_history):
                r = iv_rank(series, iv)
                rank = round(r[0], 3) if r else None
        except Exception:  # noqa: BLE001
            rank = None
        close = sig.get("close")
        stop = sig.get("sl")
        if not stop and close and sig.get("atr"):
            stop = ta.levels(close, sig.get("atr"), s)[0]
        ta_score = sig.get("ta_score") or 0.0
        selection = {
            "delta": round(float(d["delta"]), 3), "dte": c["dte"], "iv": iv, "iv_rank": rank,
            "spread_pct": round(spread, 2), "underlying_close": close or spot,
            "ta_score": ta_score, "ml_p": sig.get("ml_p"), "bid": bid, "ask": ask,
            "features": options_ml.signal_features(sig),
        }
        ml_note = ""
        if s.options_ml_enabled:
            p_win = options_ml.predict(selection["features"], selection["delta"], c["dte"], iv)
            exploring = arm is not None
            if p_win is not None and s.options_ml_min_p > 0 and p_win < s.options_ml_min_p                     and not exploring:
                return skip(f"options ML P(win) {p_win:.2f} is below {s.options_ml_min_p:.2f}", "ml")
            selection.update({"equity_ml_p": sig.get("ml_p"),
                              "ml_p": None if p_win is None else round(p_win, 4),
                              "ml_version": options_ml.model_version(), "explore": exploring,
                              "arm": list(arm) if arm else None})
            if p_win is not None:
                ml_note = f"; ML {p_win:.2f}" + (" · explore" if exploring else "")
            elif exploring:
                ml_note = "; ML n/a · explore"
        rank_txt = f"IV rank {rank:.2f}" if rank is not None else f"IV {iv:.0%}" if iv else "IV n/a"
        return {
            "symbol": c["symbol"], "side": "BUY", "quantity": qty, "limit_price": limit,
            "confidence": round(float(sig["ml_p"]), 2) if sig.get("ml_p") is not None else 0.7,
            "rationale": (f"Call on {sym}: equity gate passed (TA {ta_score:+.2f}); "
                          f"{abs(selection['delta']):.2f}Δ, {c['dte']} DTE, {rank_txt}{ml_note}"),
            "option_type": "CALL", "strike_price": c["strike"], "expiration_date": expiry,
            "position_intent": "BUY_TO_OPEN", "option_strategy": "SINGLE",
            "underlying": sym, "underlying_stop": round(float(stop), 2) if stop else None,
            "underlying_entry_close": close or spot, "selection": selection,
        }
