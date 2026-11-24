"""Hard risk limits. These run in code on every proposal and again right before
an order is sent. The model cannot change or skip them.

Every check is recorded with pass/fail so the dashboard can show the full list.

Exits (SELLs of shares you hold) are exempt from the daily trade cap: a limit
meant to stop over-trading must never block you from getting out of a position.

A second layer runs inside the Webull MCP server (WEBULL_SYMBOL_WHITELIST,
WEBULL_MAX_ORDER_NOTIONAL_USD, WEBULL_MAX_ORDER_QUANTITY), but only for single
stock orders: it does not check combo (bracket) legs, so this file is the main
guard for brackets.
"""

from __future__ import annotations

from .broker.market_data import fresh_positions
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .broker import Broker, BrokerError
from .config import Settings
from .db import DB
from .order_style import extended_spread_pct
from .market import market_is_open, now_et, now_utc, order_session, session_phase
from .portfolio import parse_position_rows

log = logging.getLogger(__name__)


def working_buys(db: DB, symbol: str | None = None, exclude_pid: int | None = None) -> list[dict]:
    """Entry BUYs sent to the broker that have not filled yet (still working).

    Two sources: open ledger BUY rows with no fill price, and proposals whose fate is unknown
    (submitting/unknown: no ledger row may exist yet). Fully or partly filled entries are not
    listed: those are held positions, handled by the add rules. ``exclude_pid`` leaves out
    the proposal being checked (re-check at execution time).
    """
    out: dict[int, dict] = {}
    for t in db.open_trades():
        if t.get("side") != "BUY" or t.get("entry_fill_price"):
            continue
        if symbol and t.get("symbol") != symbol:
            continue
        pid = t.get("proposal_id")
        if exclude_pid is not None and pid == exclude_pid:
            continue
        out[pid or -t["id"]] = {"symbol": t["symbol"], "proposal_id": pid}
    for p in db.unresolved_proposals():
        if p.get("side") != "BUY" or (symbol and p.get("symbol") != symbol):
            continue
        if exclude_pid is not None and p["id"] == exclude_pid:
            continue
        out.setdefault(p["id"], {"symbol": p["symbol"], "proposal_id": p["id"]})
    # Belt and braces (followup-correctness): a recent BUY proposal marked placed that has no
    # ledger row (e.g. settled by reconcile) is still a working entry, never a free symbol.
    try:
        since = (now_utc() - timedelta(days=1)).isoformat(timespec="seconds")
        for p in db.proposals(limit=200, symbol=symbol):
            if p.get("status") != "placed" or p.get("side") != "BUY" or p["id"] in out:
                continue
            if exclude_pid is not None and p["id"] == exclude_pid:
                continue
            if (p.get("placed_at") or p.get("created_at") or "") < since:
                continue
            if db.get_trade(p["id"]) is None:
                out[p["id"]] = {"symbol": p["symbol"], "proposal_id": p["id"]}
    except Exception:  # noqa: BLE001 - a lookup problem must not break the risk check
        log.debug("working_buys: placed-without-ledger lookup failed", exc_info=True)
    return list(out.values())


def working_buy_symbols(db: DB) -> set[str]:
    return {w["symbol"] for w in working_buys(db)}


def reprice_violation(settings: Settings, *, qty: float, px: float, sl: float | None,
                      tp: float | None, held: float = 0.0, last: float | None = None) -> str | None:
    """Re-run the size/risk rules of ``RiskManager.check`` for a BUY re-priced to ``px`` (the
    marketable-entry re-price and the entry-chase re-send). Returns the first violated rule, or
    None. ``held`` is the shares already held (for the per-symbol position cap)."""
    s = settings
    if qty * px > s.max_order_usd + 1e-9:
        return f"order value ${qty * px:,.2f} exceeds the ${s.max_order_usd:,.2f} per-order limit"
    if sl and float(sl) >= px:
        return f"stop {sl} is not below the price {px}"
    if tp and float(tp) <= px:
        return f"target {tp} is not above the price {px}"
    if s.bracket_orders and sl and tp:
        risk = qty * (px - float(sl))
        if risk > s.max_risk_per_trade_usd + 1e-9:
            return (f"${risk:,.2f} at risk to the stop exceeds the "
                    f"${s.max_risk_per_trade_usd:,.2f} limit")
    exposure = held * (last or px) + qty * px
    if exposure > s.max_position_usd + 1e-9:
        return f"position would be ${exposure:,.2f}, over the ${s.max_position_usd:,.2f} limit"
    return None


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    last_price: float | None = None
    checks: list[dict] = field(default_factory=list)


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def _is_fractional(self, qty: float) -> bool:
        """Check if quantity is fractional (not a whole number)."""
        return isinstance(qty, float) and qty != int(qty)

    def _validate_fractional(self, symbol: str, qty: float) -> tuple[bool, str]:
        """Validate fractional quantity if fractional trading is enabled."""
        s = self.s
        if not s.fractional_enabled:
            return False, "fractional trading is disabled"
        if symbol.upper() not in s.fractional_whitelist:
            return False, f"{symbol} is not on the fractional whitelist"
        if qty < 10 ** (-s.fractional_decimals):
            return False, f"quantity {qty} below minimum precision (10^-{s.fractional_decimals})"
        # Round to configured decimals
        rounded = round(qty, s.fractional_decimals)
        if rounded != qty:
            return False, f"quantity {qty} exceeds {s.fractional_decimals} decimal places"
        return True, "ok"

    async def check(self, p: dict, broker: Broker, db: DB, *, kill_switch: bool,
                    at_execution: bool, watchlist: list[str] | None = None) -> Verdict:
        s = self.s
        effective_watchlist = watchlist or s.watchlist
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        def verdict(last: float | None = None) -> Verdict:
            reasons = [c["detail"] for c in checks if not c["ok"]]
            return Verdict(not reasons, reasons, last, checks)

        symbol, side = p["symbol"], p["side"]
        qty, limit = p["quantity"], p["limit_price"]
        # Use original fractional flag from engine (before sizing/clamping)
        is_frac = p.get("_is_fractional", False)
        qty_ok = (isinstance(qty, (int, float)) and qty > 0 and
                  (not is_frac or qty >= 10 ** (-s.fractional_decimals)))
        limit_ok = isinstance(limit, (int, float)) and limit > 0

        # Cheap checks first: no network needed.
        add("Kill switch", not kill_switch, "kill switch is on" if kill_switch else "off")
        add("Watchlist", symbol in effective_watchlist,
            f"{symbol} is on the watchlist" if symbol in effective_watchlist
            else f"{symbol} is not on the watchlist")
        add("Side", side in ("BUY", "SELL"),
            side if side in ("BUY", "SELL") else f"side must be BUY or SELL, got {side!r}")
        if not qty_ok:
            add("Quantity", False,
                "quantity must be positive (fractional allowed when FRACTIONAL_ENABLED=true)")
        else:
            # Validate fractional if applicable
            if is_frac:
                frac_ok, frac_msg = self._validate_fractional(symbol, qty)
                add("Fractional", frac_ok, frac_msg)
                # Fractional shares cannot use bracket orders
                if side == "BUY" and p.get("_use_brackets", s.bracket_orders):
                    add("Bracket orders", False,
                        "bracket orders (OTOCO) are not supported for fractional quantities; "
                        "protective orders will be placed separately after fill")
            add("Quantity", qty <= s.max_order_qty,
                f"{qty:g} shares (max {s.max_order_qty})" if qty <= s.max_order_qty
                else f"quantity {qty:g} exceeds the {s.max_order_qty}-share limit per order")
        if not limit_ok:
            add("Limit price", False, "limit_price must be positive")
        elif qty_ok:
            value = qty * limit
            add("Order value", value <= s.max_order_usd,
                f"${value:,.2f} (max ${s.max_order_usd:,.2f})" if value <= s.max_order_usd
                else f"order value ${value:,.2f} exceeds the ${s.max_order_usd:,.2f} "
                     "per-order limit")
        if side == "SELL":
            add("Daily trades", True, "exits are exempt from the daily trade limit")
        elif s.max_trades_per_day <= 0:
            add("Daily trades", True, "no daily trade-count cap configured")
        else:
            used = db.count_placed_today()
            add("Daily trades", used < s.max_trades_per_day,
                f"{used} of {s.max_trades_per_day} used today" if used < s.max_trades_per_day
                else f"daily limit of {s.max_trades_per_day} placed trades reached")
        if side == "BUY" and s.bracket_orders and qty_ok and limit_ok and not is_frac:
            sl, tp = p.get("sl"), p.get("tp")
            if not sl or not tp:
                add("Stop loss", False, "no stop-loss / take-profit level could be computed")
            elif not sl < limit < tp:
                add("Stop loss", False, f"levels out of order: stop {sl} < limit {limit} < "
                                        f"target {tp} is required")
            else:
                risk = qty * (limit - sl)
                add("Risk per trade", risk <= s.max_risk_per_trade_usd,
                    f"${risk:,.2f} at risk to the stop (max ${s.max_risk_per_trade_usd:,.2f})"
                    if risk <= s.max_risk_per_trade_usd else
                    f"${risk:,.2f} at risk to the stop exceeds the "
                    f"${s.max_risk_per_trade_usd:,.2f} limit; buy fewer shares")
        if at_execution:
            session = order_session()
            if s.market_hours_only:
                open_now = market_is_open()
                session_ok = open_now
                session_detail = ("regular market open" if open_now else
                                  "US regular market is closed (MARKET_HOURS_ONLY=true)")
            else:
                session_ok = session is not None
                session_detail = (f"{session_phase()} session open ({session})" if session_ok
                                  else "no trading session is open (weekend/holiday)")
            add("Market hours", session_ok, session_detail)
            if session == "NIGHT":
                overnight_ok = (s.overnight_trading
                                and p["symbol"].upper() in set(s.overnight_symbols))
                add("Overnight symbol", overnight_ok,
                    "enabled for overnight trading" if overnight_ok else
                    f"{p['symbol']} is not in OVERNIGHT_SYMBOLS; overnight orders are "
                    "limited to that list")
            created = p.get("created_at")
            if created:
                age_min = (now_utc() - datetime.fromisoformat(created)).total_seconds() / 60
                add("Proposal age", age_min <= s.proposal_ttl_minutes,
                    f"{age_min:.0f} min old (limit {s.proposal_ttl_minutes} min)"
                    if age_min <= s.proposal_ttl_minutes else
                    f"proposal is {age_min:.0f} min old (limit {s.proposal_ttl_minutes} min)")
        if any(not c["ok"] for c in checks):
            return verdict()

        # Checks that need live data. Any failure to fetch data rejects the order.
        # One quote() call gives `last` and `stale` from the same snapshot (no mismatched pair).
        snap_stale = False
        stale_unknown = False
        try:
            if hasattr(broker, "quote"):
                q = await broker.quote(symbol)
                last = float(q.last) if q is not None and q.last else None
                snap_stale = bool(q.stale) if q is not None else False
            else:
                last = await broker.last_price(symbol)
                stale_unknown = True      # no way to tell whether this is an extended print
        except BrokerError as e:
            add("Live price", False, f"could not fetch a price for {symbol}: {e}")
            return verdict()
        if last is None:
            add("Live price", False, f"no current price available for {symbol}")
            return verdict()

        if side == "BUY" and order_session() not in (None, "CORE"):
            # Outside the regular session `last` is the extended/overnight print. With no such
            # print it is the previous regular close: a limit near it can be far from the market
            # (ADI: limit 378.75 filled at 343.00). Opening orders fail closed; exits never do.
            if snap_stale or stale_unknown:
                add("Extended-hours price", False,
                    f"No extended-hours price for {symbol}; the regular close ${last:.2f} is stale"
                    if snap_stale else
                    f"Cannot tell whether {symbol} ${last:.2f} is a live extended-hours print "
                    "(no quote/snapshot); opening orders fail closed")
                return verdict()

        cur_session = order_session()
        max_spread = float(getattr(s, "max_spread_pct_extended", 0.0) or 0.0)
        if side == "BUY" and cur_session not in (None, "CORE") and max_spread > 0                 and hasattr(broker, "get_snapshot"):
            # Exits are never blocked by this guard; no bid/ask = allowed (and noted).
            try:
                spread = extended_spread_pct(await broker.get_snapshot(symbol), cur_session)
            except BrokerError:
                spread = None
            if spread is None:
                add("Spread", True, f"no bid/ask for {symbol}; spread guard skipped")
            else:
                add("Spread", spread <= max_spread,
                    f"bid/ask spread {spread:.2f}% (limit {max_spread:.2f}%)" if spread <= max_spread
                    else f"bid/ask spread {spread:.2f}% exceeds MAX_SPREAD_PCT_EXTENDED "
                         f"{max_spread:.2f}% outside regular hours")

        sl_level = p.get("sl")
        if side == "BUY" and isinstance(sl_level, (int, float)) and sl_level > 0:
            add("Stop vs price", last > sl_level,
                f"last ${last:.2f} is above the stop ${sl_level:.2f}" if last > sl_level else
                f"price already below the stop: last ${last:.2f} <= stop ${sl_level:.2f}")

        deviation = abs(limit - last) / last * 100
        add("Price band", deviation <= s.price_band_pct,
            f"limit ${limit:.2f} is {deviation:.2f}% from last ${last:.2f} "
            f"(max {s.price_band_pct}%)")

        try:
            held = (await fresh_positions(broker)).get(symbol, 0.0)
        except BrokerError as e:
            add("Positions", False, f"could not read positions: {e}")
            return verdict(last)

        if side == "SELL":
            add("No shorting", qty <= held,
                f"selling {qty} of {held:g} held" if qty <= held
                else f"cannot sell {qty} {symbol}: account holds {held:g} (no shorting)")
        if side == "BUY":
            exposure = held * last + qty * limit
            add("Position size", exposure <= s.max_position_usd,
                f"${exposure:,.2f} in {symbol} after this order (max ${s.max_position_usd:,.2f})"
                if exposure <= s.max_position_usd else
                f"position would be ${exposure:,.2f}, over the "
                f"${s.max_position_usd:,.2f} per-symbol limit")
            if s.max_daily_loss_usd > 0:
                try:
                    pnl = await broker.day_pnl()
                except BrokerError as e:
                    add("Daily loss", False, f"could not read account balance: {e}")
                    return verdict(last)
                if pnl is None:
                    # Fail closed (QA H5): an enabled guard that cannot see today's P&L must
                    # not let a new buy through. Exits are never affected (BUY only).
                    add("Daily loss", False,
                        "day P&L unavailable; cannot verify the daily loss limit, so no new "
                        "buys (fails closed)")
                else:
                    hit = pnl <= -s.max_daily_loss_usd
                    add("Daily loss", not hit,
                        f"day P&L ${pnl:,.2f} hit the ${s.max_daily_loss_usd:,.2f} daily loss "
                        "limit; no new buys today" if hit else
                        f"day P&L ${pnl:,.2f} (stop at -${s.max_daily_loss_usd:,.2f})")
            working = working_buys(db, symbol, exclude_pid=p.get("id"))
            if working:
                n = working[0]["proposal_id"]
                ref = f"BUY #{n}" if n else "a BUY"
                add("Working entry", False,
                    f"Working entry: {ref} for {symbol} is still open at the broker; "
                    "wait for it to fill or cancel")
            if held > 0:
                await self._check_add_rules(symbol, p, held, last, broker, db, add)

        return verdict(last)

    # -- strategy.md "add at most once" / "never add to a loser" (QA H6) -----------------
    @staticmethod
    def _buys_since_open(symbol: str, p: dict, db: DB) -> int:
        """Placed or filled BUYs recorded in the ledger for the position currently open.

        Ledger BUY rows stay ``open`` until the position is closed; ``cancelled`` rows (never
        filled) are not counted. This proposal's own row (re-check at execution) is excluded.
        """
        own = p.get("id")
        n = 0
        for t in db.open_trades():
            if t.get("symbol") != symbol or t.get("side") != "BUY":
                continue
            if own is not None and t.get("proposal_id") == own:
                continue
            n += 1
        return n

    @staticmethod
    def _avg_cost(symbol: str, positions_text: str | None, db: DB) -> float | None:
        """Average cost per share: the broker's figure first, else the open ledger BUY fills."""
        if positions_text:
            try:
                for row in parse_position_rows(positions_text):
                    if row["symbol"] == symbol and row.get("cost"):
                        return float(row["cost"])
            except Exception:  # noqa: BLE001 - fall through to the ledger
                log.debug("could not parse broker cost for %s", symbol, exc_info=True)
        qty = cost = 0.0
        try:
            for t in db.open_trades():
                px = t.get("entry_fill_price")
                q = t.get("quantity_filled_real") or t.get("quantity_filled") or 0
                if t.get("symbol") == symbol and t.get("side") == "BUY" and px and q:
                    qty += q
                    cost += q * px
        except Exception:  # noqa: BLE001
            return None
        return cost / qty if qty > 0 else None

    async def _check_add_rules(self, symbol: str, p: dict, held: float, last: float,
                               broker: Broker, db: DB, add) -> None:
        s = self.s
        if s.max_adds_per_symbol >= 0:
            adds = max(0, self._buys_since_open(symbol, p, db) - 1)
            add("Max adds", adds < s.max_adds_per_symbol,
                f"add #{adds + 1} to {symbol} (at most {s.max_adds_per_symbol} add(s) per "
                "position)" if adds < s.max_adds_per_symbol else
                f"already added to {symbol} {adds} time(s); MAX_ADDS_PER_SYMBOL="
                f"{s.max_adds_per_symbol}")
        if s.block_add_to_loser:
            text = None
            try:
                text = await broker.positions_text()
            except Exception:  # noqa: BLE001 - ledger fallback below
                text = None
            avg = self._avg_cost(symbol, text, db)
            if avg is None:
                add("Add to loser", False,
                    f"average cost of {symbol} unavailable; cannot confirm the position is not "
                    "losing, so no add (fails closed)")
            else:
                add("Add to loser", last >= avg,
                    f"last ${last:,.2f} is at or above average cost ${avg:,.2f}" if last >= avg
                    else f"last ${last:,.2f} is below average cost ${avg:,.2f}; never add to a "
                         "loser (BLOCK_ADD_TO_LOSER)")


# ---------------------------------------------------------------------------
# Options Risk Manager
# ---------------------------------------------------------------------------

class OptionRiskManager:
    """Risk checks for options trading. Separate from equity RiskManager
    because options have different risk characteristics (leverage, expiration,
    assignment risk, etc.)."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def _parse_occ_symbol(self, symbol: str) -> dict | None:
        """Parse OCC option symbol: AAPL240119C00150000"""
        if len(symbol) < 15:
            return None
        # Find the 6-digit date followed by C/P
        for i in range(len(symbol) - 14):
            if symbol[i:i+6].isdigit() and symbol[i+6] in ("C", "P"):
                underlying = symbol[:i].upper()
                exp_str = symbol[i:i+6]
                option_type = "CALL" if symbol[i+6] == "C" else "PUT"
                strike_str = symbol[i+7:i+15]
                try:
                    strike = int(strike_str) / 1000
                except ValueError:
                    return None
                expiration = f"20{exp_str[:2]}-{exp_str[2:4]}-{exp_str[4:6]}"
                return {
                    "underlying": underlying,
                    "expiration": expiration,
                    "option_type": option_type,
                    "strike": strike,
                }
        return None

    def _days_to_expiration(self, expiration: str) -> int:
        """Calculate days to expiration from YYYY-MM-DD."""
        from datetime import date
        try:
            exp_date = date.fromisoformat(expiration)
            today = now_et().date()      # the exchange's date, not the host's local date
            return (exp_date - today).days
        except ValueError:
            return -1

    def check_single_leg(self, p: dict) -> tuple[bool, list[str]]:
        """Check a single-leg option order proposal.

        p should contain: symbol (OCC), side (BUY/SELL), quantity (contracts),
        limit_price (premium per contract), option_type, strike_price, expiration_date,
        position_intent (BUY_TO_OPEN, etc.)
        """
        s = self.s
        reasons: list[str] = []

        intent = p.get("position_intent", "BUY_TO_OPEN")
        valid_intents = {"BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE"}
        if intent not in valid_intents:
            reasons.append(f"invalid position_intent: {intent}")
            return False, reasons
        closing = intent in ("BUY_TO_CLOSE", "SELL_TO_CLOSE")

        # A side that contradicts the intent is ambiguous: refuse it.
        side = p.get("side")
        if side is not None and side != intent.split("_", 1)[0]:
            reasons.append(f"side {side!r} contradicts position_intent {intent}")
            return False, reasons

        # Parse OCC symbol
        parsed = self._parse_occ_symbol(p.get("symbol") or "")
        if not parsed:
            reasons.append(f"invalid OCC symbol format: {p.get('symbol')}")
            return False, reasons

        qty = p.get("quantity", 0)
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            reasons.append("quantity must be a positive integer (contracts)")
            return False, reasons
        premium = p.get("limit_price", 0)
        if (isinstance(premium, bool) or not isinstance(premium, (int, float))
                or not math.isfinite(premium) or premium <= 0):
            reasons.append("limit_price (premium) must be a positive, finite number")
            return False, reasons

        if closing:
            # Exits are never blocked (contracts section 0): a closing order only has to be a
            # well-formed order. The options-enabled flag, underlying list, DTE window and
            # notional cap are limits on OPENING risk. The broker guard also lets it through.
            return True, reasons

        # --- opening orders: every check below fails closed -----------------------------
        if not s.options_enabled:
            reasons.append("options trading is disabled (OPTIONS_ENABLED=false)")
            return False, reasons

        underlying = parsed["underlying"]
        expiration = parsed["expiration"]

        if underlying not in s.options_allowed_underlyings:
            reasons.append(f"underlying {underlying} not in allowed underlyings")
            return False, reasons

        dte = self._days_to_expiration(expiration)
        if dte < 0:
            reasons.append(f"invalid or past expiration date: {expiration}")
            return False, reasons
        if dte < s.options_min_dte:
            reasons.append(f"expiration too near: {dte} DTE (min {s.options_min_dte})")
            return False, reasons
        if dte > s.options_max_dte:
            reasons.append(f"expiration too far: {dte} DTE (max {s.options_max_dte})")
            return False, reasons

        if qty > s.options_max_contracts_per_order:
            reasons.append(f"quantity {qty} exceeds max contracts per order ({s.options_max_contracts_per_order})")
            return False, reasons

        notional = qty * premium * 100  # 100 shares per contract
        if notional > s.options_max_notional_usd:
            reasons.append(f"notional ${notional:,.2f} exceeds max ${s.options_max_notional_usd:,.2f}")
            return False, reasons

        if "SINGLE" not in s.options_allowed_strategies:
            reasons.append("SINGLE leg strategy not allowed")
            return False, reasons

        # A bare short option has open-ended (calls) or very large (puts) loss. Only a caller
        # that has verified cover may set ``_covered``; nothing in this module can verify it.
        if intent == "SELL_TO_OPEN" and p.get("_covered") is not True:
            reasons.append("single-leg SELL_TO_OPEN (short option) is refused: cover is not "
                           "verified (naked short options are not allowed)")
            return False, reasons

        return True, reasons

    def check_strategy(self, legs: list[dict]) -> tuple[bool, list[str]]:
        """Check a multi-leg option strategy order.

        Each leg should have: symbol, side, quantity, option_type,
        strike_price, expiration_date, position_intent, option_strategy.
        """
        s = self.s
        reasons: list[str] = []

        if not s.options_enabled:
            reasons.append("options trading is disabled (OPTIONS_ENABLED=false)")
            return False, reasons

        if len(legs) < 2:
            reasons.append("multi-leg strategies require at least 2 legs")
            return False, reasons

        # Check all legs have same strategy type
        strategies = set(leg.get("option_strategy", "VERTICAL") for leg in legs)
        if len(strategies) > 1:
            reasons.append(f"all legs must have same strategy, got: {strategies}")
            return False, reasons

        strategy = strategies.pop()
        if strategy not in s.options_allowed_strategies:
            reasons.append(f"strategy {strategy} not in allowed strategies")
            return False, reasons

        # Validate leg count for strategy
        min_legs, max_legs = {
            "SINGLE": (1, 1),
            "COVERED_STOCK": (2, 2),
            "STRADDLE": (2, 2),
            "STRANGLE": (2, 2),
            "VERTICAL": (2, 2),
            "CALENDAR": (2, 2),
            "DIAGONAL": (2, 2),
            "BUTTERFLY": (3, 4),
            "CONDOR": (4, 4),
            "COLLAR_WITH_STOCK": (3, 3),
            "IRON_BUTTERFLY": (4, 4),
            "IRON_CONDOR": (4, 4),
        }.get(strategy, (2, 4))

        if len(legs) < min_legs or len(legs) > max_legs:
            reasons.append(f"{strategy} requires {min_legs}-{max_legs} legs, got {len(legs)}")
            return False, reasons

        # One underlying per strategy (covered stock legs are validated separately).
        underlyings = {(self._parse_occ_symbol(l.get("symbol", "")) or {}).get("underlying")
                       for l in legs}
        if len(underlyings) > 1:
            reasons.append(f"all legs must share one underlying, got: "
                           f"{sorted(u or '?' for u in underlyings)}")
            return False, reasons

        # Validate each leg
        total_notional = 0.0
        for i, leg in enumerate(legs):
            parsed = self._parse_occ_symbol(leg.get("symbol", ""))
            if not parsed:
                reasons.append(f"leg {i}: invalid OCC symbol")
                continue

            if parsed["underlying"] not in s.options_allowed_underlyings:
                reasons.append(f"leg {i}: underlying not allowed")
                continue

            dte = self._days_to_expiration(parsed["expiration"])
            if dte < s.options_min_dte or dte > s.options_max_dte:
                reasons.append(f"leg {i}: DTE {dte} out of range [{s.options_min_dte}, {s.options_max_dte}]")
                continue

            if leg.get("position_intent", "BUY_TO_OPEN") not in {
                    "BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE"}:
                reasons.append(f"leg {i}: invalid position_intent")
                continue

            qty = leg.get("quantity", 0)
            if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                reasons.append(f"leg {i}: quantity must be positive integer")
                continue
            if qty > s.options_max_contracts_per_order:
                reasons.append(f"leg {i}: quantity exceeds max contracts")
                continue

            premium = leg.get("limit_price", 0)
            if (isinstance(premium, bool) or not isinstance(premium, (int, float))
                    or not math.isfinite(premium) or premium <= 0):
                reasons.append(f"leg {i}: premium must be positive and finite")
                continue

            total_notional += qty * premium * 100

        if total_notional > s.options_max_notional_usd:
            reasons.append(f"total notional ${total_notional:,.2f} exceeds max ${s.options_max_notional_usd:,.2f}")

        return len(reasons) == 0, reasons
