"""Read-only daily log of Webull scanners (no orders): builds a point-in-time history."""

from __future__ import annotations

import logging
import re

from .market import is_trading_day, now_et

log = logging.getLogger("app.scanner_log")

AFTER_CLOSE_HOUR, AFTER_CLOSE_MIN = 16, 30
KV_KEY = "scanner_log_day"
# scanner name -> (MCP tool, arguments)
SCANNERS: dict[str, tuple[str, dict]] = {
    "most_active": ("get_most_active", {"category": "US_STOCK"}),
    "gainers": ("get_gainers_losers", {"rank_type": "DAY_1", "direction": "DESC"}),
    "losers": ("get_gainers_losers", {"rank_type": "DAY_1", "direction": "ASC"}),
    "week52_high": ("get_52_week_high_low", {"category": "US_STOCK", "rank_type": "NEW_HIGH"}),
}
_HEAD = re.compile(r"^\s{1,}([A-Z][A-Z0-9.\-]{0,9})\s+-?\s*.*\(instrument_id:", re.M)
_FIELD = re.compile(r"([A-Za-z0-9() ]+?):\s*(\S+)")
_KEYS = {"Price": "price", "Change Ratio": "change_pct", "Volume": "volume",
         "52W Change Ratio": "change_52w", "Market Value": "market_value",
         "Relative Volume 10d": "rel_volume_10d", "High": "high", "Low": "low"}


def _num(s: str):
    try:
        return float(s.replace(",", "").rstrip("%"))
    except ValueError:
        return None


def parse_scanner(text: str) -> list[dict]:
    """Parse the MCP formatter text into [{symbol, price, change_pct, volume, ...}] in rank order."""
    rows: list[dict] = []
    cur: dict | None = None
    for line in (text or "").splitlines():
        m = _HEAD.match(line)
        if m:
            cur = {"symbol": m.group(1)}
            rows.append(cur)
            continue
        if cur is None:
            continue
        for k, v in _FIELD.findall(line):
            key = _KEYS.get(k.strip())
            if key and key not in cur and (n := _num(v)) is not None:
                cur[key] = n
    return rows


async def log_scanners(eng) -> dict:
    """Once per trading day after 16:30 ET. Never raises. Returns {scanner: n_rows}."""
    try:
        now = now_et()
        today = now.date().isoformat()
        if getattr(eng.s, "broker_mode", "") == "mock" or not is_trading_day(now.date()):
            return {}
        if (now.hour, now.minute) < (AFTER_CLOSE_HOUR, AFTER_CLOSE_MIN):
            return {}
        if eng.db.get_kv(KV_KEY) == today:
            return {}
        out: dict = {}
        errors = 0
        for name, (tool, args) in SCANNERS.items():
            try:
                rows = parse_scanner(await eng.broker.call(tool, **args))
                if rows:
                    eng.db.save_scanner_snapshot(today, name, rows)
                    out[name] = len(rows)
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.warning("scanner %s skipped: %r", name, e)
        # Done for today when anything was saved, or when every call answered but with no rows
        # (a "no data" reply would otherwise re-run the 4 calls every 15 min until midnight).
        # Retry later only when calls actually failed.
        if out or errors == 0:
            eng.db.set_kv(KV_KEY, today)
            if not out:
                log.info("scanner log: all scanners returned no rows for %s", today)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("scanner log failed: %r", e)
        return {}
