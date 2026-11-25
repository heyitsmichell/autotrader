"""Option consensus gate (fail closed) and the daily ATM IV history behind the IV rank.

H2/M7: every missing or stale datum REJECTS the order. H3: IV rank is a true rank over the
stored daily ATM IV history; with too little history the raw IV is compared and the check says so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date

from ..broker import BrokerError
from ..market import now_et
from ..quotes import Quote
from .util import parse_occ

log = logging.getLogger("app.engine")

_OCC_TOKEN = re.compile(r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b")
ATM_IV_RETRY_SECONDS = 900.0


def normalize_iv(iv) -> float | None:
    """IV as a 0-1 fraction (brokers report either 0.30 or 30)."""
    try:
        v = float(iv)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0:
        return None
    return v / 100.0 if v > 1.0 else v


def iv_rank(series: list[float], iv: float) -> tuple[float, float] | None:
    """(rank, percentile) of ``iv`` within ``series``; None when the series has no range.

    rank = (iv - min) / (max - min), clamped to [0, 1]; percentile = share of days at or below iv.
    """
    if not series:
        return None
    lo, hi = min(series), max(series)
    if hi - lo <= 1e-12:
        return None
    rank = min(1.0, max(0.0, (iv - lo) / (hi - lo)))
    pct = sum(1 for x in series if x <= iv) / len(series)
    return rank, pct


class OptionsFlowMixin:
    # -- ATM IV history (H3) ---------------------------------------------------------
    async def record_atm_iv(self, underlying: str) -> float | None:
        """Record today's ATM IV for ``underlying`` (once per day) and return it.

        ATM = the call and put with the strike nearest the spot price in the nearest expiry
        inside the entry DTE window; the stored value is the average of their IVs (0-1 fraction).
        Returns None (and records nothing) when already recorded today or when any step fails:
        the gate then falls back to the raw IV and says so.
        """
        underlying = underlying.upper()
        day = now_et().date().isoformat()
        if self._atm_iv_day.get(underlying) == day:
            return None
        if self.db.get_kv(f"atm_iv_day:{underlying}") == day:
            self._atm_iv_day[underlying] = day
            return None
        last_try = self._atm_iv_attempt.get(underlying)
        if last_try is not None and time.monotonic() - last_try < ATM_IV_RETRY_SECONDS:
            return None
        self._atm_iv_attempt[underlying] = time.monotonic()
        try:
            spot = await self.broker.last_price(underlying)
            if not spot or spot <= 0:
                return None
            text = await self.broker.option_contracts([underlying])
            today = now_et().date()
            chain: dict[str, list[dict]] = {}
            for tok in dict.fromkeys(_OCC_TOKEN.findall(text or "")):
                occ = parse_occ(tok)
                if not occ or occ["underlying"] != underlying:
                    continue
                dte = (date.fromisoformat(occ["expiration"]) - today).days
                if self.s.options_min_dte <= dte <= self.s.options_max_dte:
                    chain.setdefault(occ["expiration"], []).append({**occ, "symbol": tok})
            if not chain:
                return None
            expiry = min(chain)         # nearest expiry in the window (ISO dates sort)
            legs = []
            for kind in ("CALL", "PUT"):
                cands = [c for c in chain[expiry] if c["option_type"] == kind]
                if cands:
                    legs.append(min(cands, key=lambda c: abs(c["strike"] - spot))["symbol"])
            if not legs:
                return None
            snaps = await self.broker.parsed_option_snapshots(legs)
            ivs = [v for v in (normalize_iv((snaps.get(sym) or {}).get("iv")) for sym in legs)
                   if v is not None]
            if not ivs:
                return None
            iv = sum(ivs) / len(ivs)
            self.db.record_atm_iv(underlying, day, iv)
            self.db.set_kv(f"atm_iv_day:{underlying}", day)
            self._atm_iv_day[underlying] = day
            self.db.log("info", f"Recorded ATM IV for {underlying}: {iv:.1%} ({expiry})")
            return iv
        except (BrokerError, ValueError) as e:
            log.warning("ATM IV record for %s failed: %s", underlying, e)
            return None

    def _iv_check(self, add, underlying: str | None, iv_pct: float, is_long: bool) -> None:
        """Add the IV check: a true rank when enough history exists, else raw IV, labelled."""
        s = self.s
        try:
            series = self.db.atm_iv_series(underlying, 252) if underlying else []
        except Exception:  # noqa: BLE001
            series = []
        n, need = len(series), int(s.options_iv_rank_min_history)
        ranked = iv_rank(series, iv_pct) if n >= need else None
        if ranked is not None:
            rank, pct = ranked
            if is_long:
                ok = rank <= s.options_iv_rank_max
                add("IV Rank (long)", ok,
                    f"IV rank {rank:.0%} (percentile {pct:.0%}, {n}d ATM IV history) "
                    f"{'within' if ok else 'exceeds'} max {s.options_iv_rank_max:.0%}",
                    value=round(rank, 3), threshold=s.options_iv_rank_max)
            else:
                ok = rank >= s.options_iv_rank_min
                add("IV Rank (short)", ok,
                    f"IV rank {rank:.0%} (percentile {pct:.0%}, {n}d ATM IV history) "
                    f"{'above' if ok else 'below'} min {s.options_iv_rank_min:.0%}",
                    value=round(rank, 3), threshold=s.options_iv_rank_min)
            return
        label = f"IV (raw, insufficient history {n}/{need})"
        if is_long:
            ok = iv_pct <= s.options_iv_rank_max
            add(label, ok, f"{label}: IV {iv_pct:.0%} {'within' if ok else 'exceeds'} max "
                           f"{s.options_iv_rank_max:.0%} (raw IV, not a rank)",
                value=round(iv_pct, 3), threshold=s.options_iv_rank_max)
        else:
            ok = iv_pct >= s.options_iv_rank_min
            add(label, ok, f"{label}: IV {iv_pct:.0%} {'above' if ok else 'below'} min "
                           f"{s.options_iv_rank_min:.0%} (raw IV, not a rank)",
                value=round(iv_pct, 3), threshold=s.options_iv_rank_min)

    # -- the gate ----------------------------------------------------------------------
    async def _option_consensus_gate(self, p: dict) -> dict:
        """Evaluate the option consensus gate: quote integrity and age, IV rank, delta, liquidity.

        FAILS CLOSED: a missing snapshot, a missing/zero bid or ask, a crossed or locked market,
        a spread above the maximum, missing IV/delta/volume/open interest, or a quote older than
        ``options_quote_max_age_seconds`` each reject the order with a reason.

        Returns a dict with: ok (bool), checks (list), sl (None), tp (None).
        """
        s = self.s
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str, value=None, threshold=None) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail,
                           "value": value, "threshold": threshold})

        def result() -> dict:
            return {"ok": all(c["ok"] for c in checks), "checks": checks, "sl": None, "tp": None}

        symbol = p["symbol"]
        position_intent = p.get("position_intent", "BUY_TO_OPEN")
        is_long = position_intent in ("BUY_TO_OPEN", "BUY_TO_CLOSE")
        occ = parse_occ(symbol)
        underlying = occ["underlying"] if occ else None

        # Best effort, at most once a day per underlying: feeds the IV rank history.
        if underlying:
            try:
                await asyncio.wait_for(self.record_atm_iv(underlying), 20)
            except Exception as e:  # noqa: BLE001 - the gate must not depend on it
                log.debug("ATM IV recording skipped: %r", e)

        snapshot: dict | None = None
        err: Exception | None = None
        try:
            snaps = await self.broker.parsed_option_snapshots([symbol])
            snapshot = (snaps or {}).get(symbol)
        except Exception as e:  # noqa: BLE001
            err = e
            log.warning("Option consensus gate: could not fetch snapshot for %s: %s", symbol, e)
        if not snapshot:
            add("Option quote", False,
                f"no option snapshot for {symbol} ({err if err else 'empty response'}): failing closed")
            return result()

        q = Quote(symbol=symbol, bid=snapshot.get("bid"), ask=snapshot.get("ask"),
                  last=snapshot.get("last"), as_of=float(snapshot.get("as_of") or 0.0),
                  iv=snapshot.get("iv"), delta=snapshot.get("delta"),
                  volume=snapshot.get("volume"), open_interest=snapshot.get("open_interest"))

        # Quote / IV / Greeks age (one timestamp covers all three in a snapshot).
        max_age = float(s.options_quote_max_age_seconds)
        if not q.as_of:
            add("Quote age", False, "snapshot has no as_of timestamp: failing closed",
                value=None, threshold=max_age)
        else:
            age = q.age()
            add("Quote age", age <= max_age,
                f"quote/IV/Greeks {age:.1f}s old {'<=' if age <= max_age else '>'} max {max_age:g}s",
                value=round(age, 1), threshold=max_age)

        # Two-sided, uncrossed market and spread.
        bid, ask = q.bid, q.ask
        if bid is None or ask is None:
            add("Bid-Ask Spread", False, f"bid/ask missing (bid={bid}, ask={ask}): failing closed")
        elif bid <= 0 or ask <= 0:
            add("Bid-Ask Spread", False,
                f"zero or negative bid/ask (bid={bid}, ask={ask}): no two-sided market")
        elif not q.two_sided:
            add("Bid-Ask Spread", False, f"crossed or locked market (bid={bid}, ask={ask})")
        else:
            spread_pct = q.spread_pct * 100
            ok = spread_pct <= s.options_max_bid_ask_pct
            add("Bid-Ask Spread", ok,
                f"spread {spread_pct:.1f}% {'<=' if ok else '>'} max {s.options_max_bid_ask_pct}%",
                value=round(spread_pct, 1), threshold=s.options_max_bid_ask_pct)

        # IV (rank, or labelled raw IV)
        iv_pct = normalize_iv(q.iv)
        if iv_pct is None:
            add("IV Rank", False, "IV missing or zero: failing closed")
        else:
            self._iv_check(add, underlying, iv_pct, is_long)

        # Delta
        delta = q.delta
        if delta is None:
            add("Delta", False, "delta missing: failing closed")
        elif is_long:
            abs_delta = abs(delta)
            ok = s.options_delta_min <= abs_delta <= s.options_delta_max
            add("Delta", ok,
                f"delta {delta:.2f} (abs {abs_delta:.2f}) {'in' if ok else 'outside'} range "
                f"[{s.options_delta_min}, {s.options_delta_max}]",
                value=round(delta, 3), threshold=f"[{s.options_delta_min}, {s.options_delta_max}]")
        else:
            add("Delta (short)", True, f"delta {delta:.2f} (short position)",
                value=round(delta, 3), threshold=None)

        # Liquidity
        volume = q.volume
        if volume is None:
            add("Volume", False, "volume missing: failing closed")
        else:
            ok = volume >= s.options_min_volume
            add("Volume", ok,
                f"volume {volume:,.0f} {'>=' if ok else '<'} min {s.options_min_volume:,}",
                value=int(volume), threshold=s.options_min_volume)
        oi = q.open_interest
        if oi is None:
            add("Open Interest", False, "open interest missing: failing closed")
        else:
            ok = oi >= s.options_min_open_interest
            add("Open Interest", ok,
                f"OI {oi:,.0f} {'>=' if ok else '<'} min {s.options_min_open_interest:,}",
                value=int(oi), threshold=s.options_min_open_interest)

        return result()

    async def _create_option_proposal(self, pid: int | None, p: dict, gate: dict,
                                       verdict: any, run_id: int) -> str:
        """Create an option proposal (bypasses equity sizing and risk checks)."""
        # For options, we already did option risk check, no equity risk check needed
        # No consensus gate for options yet (TODO: IV rank, delta, liquidity filters)
        # No fractional for options - contracts are whole numbers

        # Prepare extra fields for options
        extra = {
            "signal_id": None,
            "sl": p.get("underlying_stop"),     # the STOCK's stop; option_exits closes on it
            "tp": None,
            "ml_p": (p.get("selection") or {}).get("ml_p"),
            "consensus": gate["checks"],
            "risk_checks": [],
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
                "underlying": p.get("underlying"),
                "underlying_stop": p.get("underlying_stop"),
                "underlying_entry_close": p.get("underlying_entry_close"),
                "selection": p.get("selection"),
            }),
            "strategy_type": p.get("option_strategy", "SINGLE"),
            "legs": json.dumps(p.get("legs")) if p.get("legs") else None,
        }

        # Use provided pid or create new
        if pid is None:
            pid = self.db.add_proposal(
                run_id, p, "pending", "", p["limit_price"],
                None, **extra)
        else:
            # Update existing proposal (for re-quote scenarios)
            self.db.update_proposal(pid, **extra, status="pending", reason="")

        what = f"#{pid} {p['side']} {p['quantity']} {p['symbol']} @ {p['limit_price']}"
        self.db.log("info", f"Option proposal {what} passed option risk checks")

        # Alert when a proposal enters pending (for approve mode)
        if self.trading_mode == "approve" and hasattr(self, 'alerts') and self.alerts:
            self.alerts.notify(
                f"Pending option proposal #{pid}: {p['side']} {p['quantity']} {p['symbol']} @ {p['limit_price']:.2f} "
                f"(expires in {self.s.proposal_ttl_minutes} min).",
                level="warn",
                title="Trader: Option Proposal needs approval"
            )

        then = ("will be placed automatically when this run ends" if self.trading_mode == "auto"
                else "waits for the owner's approval")
        return f"ACCEPTED option proposal #{pid}: passed option risk checks and {then}."
