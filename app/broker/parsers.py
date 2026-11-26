"""Parsers for the Webull MCP text output (formats from webull_openapi_mcp/formatters.py).

Pure functions, no I/O. Also holds the 429 detection helpers used by the transport.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone

from ..market import ET, now_utc

ERROR_PREFIXES = (
    "Validation error", "Server error", "Parameter error", "Internal error",
    "Account error", "Configuration error", "Order error",
)
STOCK_ACCOUNT_CLASSES = {
    "INDIVIDUAL_CASH", "INDIVIDUAL_MARGIN", "ROTH_IRA", "TRADITIONAL_IRA",
    "ROLLOVER_IRA", "MANAGED_ROTH_IRA", "MANAGED_TRADITIONAL_IRA",
}

# ---------------------------------------------------------------------------
# Parsers for the Webull MCP text output (formats from webull_openapi_mcp/formatters.py)
# ---------------------------------------------------------------------------

def sanitize(text: str) -> str:
    """Remove hidden text tricks before the analyst reads broker or news output.

    Research on attacks against LLM traders (e.g. "Adversarial News and Lost
    Profits", TradeTrap) hides instructions in invisible characters and
    look-alike letters. NFKC folds many look-alikes; dropping Unicode "format"
    characters removes zero-width and text-direction controls.
    """
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def strip_disclaimer(text: str) -> str:
    first, sep, rest = text.partition("\n\n")
    if sep and "Disclaimer" in first:
        return rest.strip()
    return text.strip()


def parse_accounts(text: str) -> list[dict]:
    accounts = []
    for line in text.splitlines():
        m = re.match(r"\s*\d+\.\s+ID:\s*(\S+)", line)
        if not m:
            continue
        cls = re.search(r"Class:\s*(\S+)", line)
        accounts.append({"account_id": m.group(1), "account_class": cls.group(1) if cls else ""})
    return accounts


def _num(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_price(text: str, symbol: str) -> float | None:
    m = re.search(rf"^\s*{re.escape(symbol)}\s+Price:\s*(\S+)", text, re.M | re.I)
    price = _num(m.group(1)) if m else None
    return price if price and price > 0 else None


def parse_positions(text: str) -> dict[str, float]:
    """Shares held per symbol. Options and other non-stock positions are skipped."""
    positions: dict[str, float] = {}
    for line in text.splitlines():
        if "Leg:" in line:
            continue
        kind = re.search(r"\bType:\s*(\S+)", line)
        if kind and kind.group(1).upper() not in {"EQUITY", "STOCK", "ETF"}:
            continue
        sym = re.search(r"\bSymbol:\s*(\S+)", line)
        qty = re.search(r"(?<!Available )\bQty:\s*(-?[\d.,]+)", line)
        if sym and qty:
            value = _num(qty.group(1))
            if value is not None:
                positions[sym.group(1).upper()] = positions.get(sym.group(1).upper(), 0.0) + value
    return positions


def parse_option_positions(text: str) -> list[dict]:
    """Parse option positions from broker output.
    Returns list of dicts with: symbol (OCC), underlying, option_type, strike, expiration, qty, cost_basis, mark_price, unrealized_pnl.
    """
    positions: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if "Type:" in line and "OPTION" in line.upper():
            if current:
                positions.append(current)
            current = {"symbol": None, "underlying": None, "option_type": None,
                       "strike": None, "expiration": None, "qty": 0.0,
                       "cost_basis": None, "mark_price": None, "unrealized_pnl": None}
        elif current is not None and current["symbol"] and re.search(r"\bSymbol:", line):
            # a second Symbol line is the next (non-option) position: end the block so it can
            # never lend its symbol/qty to the option
            positions.append(current)
            current = None
        elif current is not None:
            sym = re.search(r"\bSymbol:\s*(\S+)", line)
            if sym:
                current["symbol"] = sym.group(1).upper()
                # Parse OCC symbol: AAPL240119C00150000
                occ = current["symbol"]
                if len(occ) >= 15:
                    # Find where the date starts (6 digits YYMMDD)
                    for i in range(len(occ) - 14):
                        if occ[i:i+6].isdigit() and occ[i+6] in ("C", "P"):
                            current["underlying"] = occ[:i].upper()
                            exp_str = occ[i:i+6]
                            current["expiration"] = f"20{exp_str[:2]}-{exp_str[2:4]}-{exp_str[4:6]}"
                            current["option_type"] = "CALL" if occ[i+6] == "C" else "PUT"
                            strike_str = occ[i+7:i+15]
                            try:
                                current["strike"] = int(strike_str) / 1000
                            except ValueError:
                                pass
                            break
            qty = re.search(r"(?<!Available )\bQty:\s*(-?[\d.,]+)", line)
            if qty:
                current["qty"] = _num(qty.group(1)) or 0.0
            cost = re.search(r"Cost\s*[:=]\s*(-?[\d.,]+)", line, re.I)
            if cost:
                current["cost_basis"] = _num(cost.group(1))
            mark = re.search(r"Mark\s*[:=]\s*(-?[\d.,]+)", line, re.I)
            if mark:
                current["mark_price"] = _num(mark.group(1))
            pnl = re.search(r"Unrealized\s*[:=]\s*(-?[\d.,]+)", line, re.I)
            if pnl:
                current["unrealized_pnl"] = _num(pnl.group(1))
    if current:
        positions.append(current)
    return positions


def parse_option_snapshot(text: str) -> dict[str, dict]:
    """Parse option snapshot data: {symbol: {bid, ask, last, volume, open_interest, iv, delta, gamma, theta, vega}}.
    Handles both mock server format (single-line) and Webull MCP format (multi-line structured)."""
    out: dict[str, dict] = {}
    current = None

    for line in text.splitlines():
        # Webull MCP format: "  Symbol: SYMBOL" - starts a new block
        m_symbol = re.match(r"\s*Symbol:\s*([A-Z0-9]+)", line)
        if m_symbol:
            current = m_symbol.group(1)
            out[current] = out.get(current, {})
            continue

        # Mock server format: "SYMBOL  Bid: X  Ask: X  Last: X  Vol: X  OI: X  IV: X  Delta: X  Gamma: X  Theta: X  Vega: X"
        if True:  # also when a previous symbol is open: every mock line starts a new symbol
            m = re.match(r"\s*([A-Z0-9]+)\s+", line)
            if m and re.match(r"^[A-Z]{1,5}\d{6}[CP]\d{8}$", m.group(1)):
                current = m.group(1)
                out[current] = {}
                # Parse inline fields on the same line
                for key, pattern in [
                    ("bid", r"Bid:\s*(\S+)"),
                    ("ask", r"Ask:\s*(\S+)"),
                    ("last", r"Last:\s*(\S+)"),
                    ("volume", r"Vol:\s*(\S+)"),
                    ("open_interest", r"OI:\s*(\S+)"),
                    ("iv", r"IV:\s*(\S+)"),
                    ("delta", r"Delta:\s*(\S+)"),
                    ("gamma", r"Gamma:\s*(\S+)"),
                    ("theta", r"Theta:\s*(\S+)"),
                    ("vega", r"Vega:\s*(\S+)"),
                    ("as_of", r"AsOf:\s*(\S+)"),
                ]:
                    m2 = re.search(pattern, line)
                    if m2:
                        out[current][key] = _num(m2.group(1))
                continue

        # Parse fields for current symbol (both formats)
        if current and current in out:
            for key, pattern in [
                ("bid", r"Bid:\s*([\d.]+)"),
                ("ask", r"Ask:\s*([\d.]+)"),
                ("last", r"Price:\s*([\d.]+)"),
                ("volume", r"Volume:\s*([\d.]+)"),
                ("open_interest", r"OI:\s*([\d.]+)"),
                ("iv", r"IV:\s*([\d.]+)"),
                ("delta", r"Delta:\s*([-\d.]+)"),
                ("gamma", r"Gamma:\s*([-\d.]+)"),
                ("theta", r"Theta:\s*([-\d.]+)"),
                ("vega", r"Vega:\s*([-\d.]+)"),
                ("as_of", r"As ?Of:\s*([\d.]+)"),
            ]:
                m2 = re.search(pattern, line)
                if m2:
                    out[current][key] = _num(m2.group(1))

    return out


def parse_day_pnl(text: str) -> float | None:
    m = re.search(r"Day P&L:\s*(-?[\d.,]+)", text)
    return _num(m.group(1)) if m else None


def parse_snapshot(text: str) -> dict[str, dict]:
    """{symbol: {...}} from get_stock_snapshot output.

    Format (webull_openapi_mcp.formatters.format_stock_snapshot): a header line
    ``<SYM> Price: x PreClose: y ...`` (the REGULAR-session price, i.e. the previous close in
    premarket/after-hours/overnight), then optional continuation lines
    ``ExtHr Price: x High: .. Vol: ..`` (premarket + after-hours last) and ``OVN Price: x ...``
    (overnight last). The text carries no per-print timestamps.

    Keys: ``price`` and ``regular_price`` (regular session), ``ext_price``/``ext_volume``,
    ``ovn_price``/``ovn_volume`` (None when absent or 0), ``prev_close``, ``volume`` (regular
    day total). Choosing the price for the current session is done by ``live_price_for_session``.
    """
    out: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        if current:
            # Quote lines: "Bid: x x N  Ask: y x M" (regular) and "OVN Bid: ... OVN Ask: ..."
            for key, rx in (("bid", r"(?<!OVN )(?<!OVN  )Bid:\s*([\d.]+)"),
                            ("ask", r"(?<!OVN )(?<!OVN  )Ask:\s*([\d.]+)"),
                            ("ovn_bid", r"OVN\s+Bid:\s*([\d.]+)"),
                            ("ovn_ask", r"OVN\s+Ask:\s*([\d.]+)")):
                bm = re.search(rx, line)
                if bm:
                    v = _num(bm.group(1))
                    out[current][key] = v if v and v > 0 else None
        for tag, key in (("ExtHr", "ext"), ("OVN", "ovn")):
            em = re.match(rf"\s*{tag}\s+Price:\s*(\S+)", line)
            if em:
                if current:
                    px = _num(em.group(1))
                    out[current][f"{key}_price"] = px if px and px > 0 else None
                    v = re.search(r"Vol:\s*(\S+)", line)
                    out[current][f"{key}_volume"] = _num(v.group(1)) if v else None
                break
        else:
            m = re.match(r"\s*([A-Za-z0-9.\-]+)\s+Price:\s*(\S+)", line)
            if m:
                current = m.group(1).upper()
                pre_m = re.search(r"PreClose:\s*(\S+)", line)
                prev_close = _num(pre_m.group(1)) if pre_m else None
                px = _num(m.group(2))
                out[current] = {"price": px, "regular_price": px, "ext_price": None,
                                "ovn_price": None, "volume": None, "prev_close": prev_close,
                                "bid": None, "ask": None, "ovn_bid": None, "ovn_ask": None}
                continue
            if current and out[current]["volume"] is None and "Vol:" in line:
                v = re.search(r"Vol:\s*(\S+)", line)
                out[current]["volume"] = _num(v.group(1)) if v else None
    return out


# A session's FIRST minutes: the snapshot has no per-print timestamp, so the ExtHr / OVN field can
# still hold the previous window's last print (this morning's premarket right after 16:00; the
# prior night's ~16h-old overnight print at 20:00; yesterday's after-hours print at 04:00).
# Known limitation: within this window we cannot tell a fresh print from a leftover one, so an
# extended/overnight price that differs from the regular price by more than LEFTOVER_TOLERANCE_PCT
# (or any NIGHT price at all) is marked stale. Stale only blocks BUYs; exits are never blocked.
SESSION_OPEN_GRACE_MINUTES = 15
LEFTOVER_TOLERANCE_PCT = 1.0


def _in_session_open_window(session: str | None, now=None) -> bool:
    et = (now or now_utc()).astimezone(ET)
    minutes = et.hour * 60 + et.minute
    starts = {"ALL": (4 * 60, 16 * 60), "NIGHT": (20 * 60,)}.get(session or "", ())
    return any(0 <= minutes - st < SESSION_OPEN_GRACE_MINUTES for st in starts)


def live_price_for_session(snap: dict, session: str | None, now=None) -> tuple[float | None, str, bool]:
    """(price, source, stale) for the current order session (``app.market.order_session()``).

    CORE -> regular price. ALL (premarket/after-hours) -> ExtHr price; NIGHT -> OVN price.
    When the session has no print yet the regular price is returned with ``stale=True``: it is
    the previous close and must never anchor a new opening order. Closed market (None): the
    latest extended print if any, not stale (nothing can be placed anyway).

    The first SESSION_OPEN_GRACE_MINUTES of ALL (from 04:00 and 16:00 ET) and NIGHT (20:00 ET)
    may still carry the previous window's last print; see the note above. When unsure: stale.
    """
    reg, ext, ovn = snap.get("regular_price", snap.get("price")), snap.get("ext_price"), snap.get("ovn_price")
    leftover_risk = _in_session_open_window(session, now)

    def _suspect(px) -> bool:
        if not leftover_risk:
            return False
        if session == "NIGHT" or not reg:
            return True
        return abs(px - reg) / reg * 100.0 > LEFTOVER_TOLERANCE_PCT

    if session == "CORE":
        return reg, "regular", False
    if session == "NIGHT":
        if ovn:
            return ovn, "overnight", _suspect(ovn)
        return reg, "regular", True
    if session == "ALL":
        if ext:
            return ext, "extended", _suspect(ext)
        return reg, "regular", True
    best = ovn or ext
    return (best, "extended", False) if best else (reg, "regular", False)


def parse_bar_time(raw: str) -> datetime | None:
    """Bar timestamps may be epoch seconds/ms or ISO text. Naive times are taken as UTC."""
    raw = raw.strip()
    if re.fullmatch(r"\d{10,13}", raw):
        ts = int(raw)
        return datetime.fromtimestamp(ts / 1000 if ts > 10**11 else ts, timezone.utc)
    s = raw.replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_bars(text: str) -> list[dict]:
    """OHLCV bars from get_stock_bars output, oldest first."""
    bars: dict[datetime, dict] = {}
    for line in text.splitlines():
        if "O:" not in line or "C:" not in line:
            continue
        head, rest = line.split("O:", 1)
        tokens = head.split()
        t = parse_bar_time(" ".join(tokens))
        if t is None and len(tokens) > 1:  # flat format starts with the symbol
            t = parse_bar_time(" ".join(tokens[1:]))
        fields = dict(re.findall(r"\b(H|L|C|Vol):\s*(\S+)", rest))
        o = _num(rest.split()[0]) if rest.split() else None
        h, low, c = (_num(fields.get(k, "")) for k in ("H", "L", "C"))
        if t is None or None in (o, h, low, c):
            continue
        bars[t] = {"time": t, "open": o, "high": h, "low": low, "close": c,
                   "volume": _num(fields.get("Vol", "")) or 0.0}
    return [bars[t] for t in sorted(bars)]


_BAR_GROUP_RE = re.compile(r"^\s*-{2,}\s*(\S+?)\s*-{2,}\s*$")


def parse_bars_by_symbol(text: str) -> dict[str | None, list[dict]]:
    """Per-symbol bars from a (batch) get_stock_bars reply, oldest first.

    Handles the grouped layout (``--- SYM ---`` header then bar lines; real MCP and mock) and the
    flat layout (symbol as line prefix). Bars with no symbol at all land under key ``None``.
    """
    lines: dict[str | None, list[str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        m = _BAR_GROUP_RE.match(line)
        if m:
            cur = m.group(1).upper()
            lines.setdefault(cur, [])
            continue
        if "O:" not in line or "C:" not in line:
            continue
        key = cur
        if key is None:
            head = line.split("O:", 1)[0].split()
            if len(head) > 1 and parse_bar_time(" ".join(head)) is None:
                key = head[0].upper()
        lines.setdefault(key, []).append(line)
    return {k: parse_bars("\n".join(v)) for k, v in lines.items()}


def bar_day(t: datetime) -> date:
    """Trading date of a daily bar. Midnight-UTC stamps mean "this date"; others use ET."""
    t = t.astimezone(timezone.utc)
    if (t.hour, t.minute, t.second) == (0, 0, 0):
        return t.date()
    return t.astimezone(ET).date()


_RATE_LIMIT_RE = re.compile(r"(?<![\d.$])429(?![\d.])|rate.?limit|too many requests", re.I)
_RETRY_AFTER_RE = re.compile(r"retry[- ]after\D{0,3}(\d+(?:\.\d+)?)", re.I)


def is_rate_limited(text: str) -> bool:
    """True for HTTP 429 / 'too many requests'. A bare '429' inside a price does not count."""
    return bool(_RATE_LIMIT_RE.search(text or ""))


def retry_after_seconds(text: str) -> float | None:
    m = _RETRY_AFTER_RE.search(text or "")
    return float(m.group(1)) if m else None

