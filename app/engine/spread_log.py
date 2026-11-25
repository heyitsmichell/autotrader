"""Option spread logger: samples real bid/ask of the auto-entry contracts at set ET times.

Never places orders and never blocks trading (runs as a background task; every failure is a
debug log). Rows land in option_spreads so OPTIONS_ENTRY_SKIP_OPEN_CLOSE_MINUTES can be chosen
from data. One batched snapshot call per sample time; contract lists are cached per day.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, timezone

from ..market import OPEN, now_et, order_session
from .util import parse_occ

log = logging.getLogger("app.engine")
_OCC_TOKEN = re.compile(r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b")


def parse_times(raw: str) -> list[str]:
    out = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", part):
            h, m = part.split(":")
            if int(h) < 24 and int(m) < 60:
                out.append(f"{int(h):02d}:{m}")
    return sorted(set(out))


def due_times(times: list[str], now_hhmm: str, done: set[str]) -> list[str]:
    """The latest listed time at or before now that is not done yet (older missed ones drop)."""
    ready = [t for t in times if t <= now_hhmm and t not in done]
    return ready[-1:] if ready else []


def _stats(rs: list[dict], max_pct: float) -> dict | None:
    vals = sorted(r["spread_pct"] for r in rs if r.get("spread_pct") is not None)
    if not vals:
        return None
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    p75 = vals[min(n - 1, int(round(0.75 * (n - 1))))]
    return {"n": n, "median_spread_pct": round(med, 2), "p75_spread_pct": round(p75, 2),
            "pct_pass": round(100 * sum(v <= max_pct for v in vals) / n, 1)}


def _buckets(rs: list[dict], max_pct: float) -> list[dict]:
    by: dict[int, list] = {}
    for r in rs:
        if r.get("minutes_from_open") is not None:
            by.setdefault(int(round(r["minutes_from_open"])), []).append(r)
    out = []
    for k, v in sorted(by.items()):
        st = _stats(v, max_pct)
        if st:
            out.append({"minutes_from_open": k, **st})
    return out


def summarize(rows: list[dict], max_pct: float) -> dict:
    """Bucket by minutes_from_open: median / p75 spread_pct and share passing max_pct."""
    unders = sorted({r["underlying"] for r in rows})
    return {"rows": len(rows), "days": len({str(r["ts"])[:10] for r in rows}),
            "max_bid_ask_pct": max_pct, "overall": _buckets(rows, max_pct),
            "by_underlying": {u: _buckets([r for r in rows if r["underlying"] == u], max_pct)
                              for u in unders}}


class SpreadLogger:
    def __init__(self, engine) -> None:
        self.eng = engine
        self._chains: dict[str, tuple[str, dict]] = {}     # underlying -> (day, expiry -> calls)
        self.running = False

    async def maybe_sample(self) -> int:
        eng, s = self.eng, self.eng.s
        if not s.options_spread_log or not s.options_enabled or self.running:
            return 0
        try:
            if order_session() != "CORE":
                return 0
            now = now_et()
            day = now.date().isoformat()
            done = set(eng.db.get_kv(f"spread_log_done:{day}", []) or [])
            due = due_times(parse_times(s.options_spread_log_times), now.strftime("%H:%M"), done)
            if not due:
                return 0
            done.add(due[0])
            eng.db.set_kv(f"spread_log_done:{day}", sorted(done))
        except Exception as e:  # noqa: BLE001
            log.debug("spread log check failed: %s", e)
            return 0
        self.running = True
        try:
            return await self.sample(now)
        except Exception as e:  # noqa: BLE001
            log.debug("spread log failed: %s", e)
            return 0
        finally:
            self.running = False

    async def _calls(self, und: str, today: date) -> dict:
        day = today.isoformat()
        hit = self._chains.get(und)
        if hit and hit[0] == day:
            return hit[1]
        s = self.eng.s
        text = await self.eng.broker.option_contracts([und])
        chain: dict[str, list] = {}
        for tok in dict.fromkeys(_OCC_TOKEN.findall(text or "")):
            occ = parse_occ(tok)
            if occ and occ["underlying"] == und and occ["option_type"] == "CALL":
                dte = (date.fromisoformat(occ["expiration"]) - today).days
                if s.options_min_dte <= dte <= s.options_max_dte:
                    chain.setdefault(occ["expiration"], []).append({**occ, "symbol": tok, "dte": dte})
        self._chains[und] = (day, chain)
        return chain

    async def sample(self, now) -> int:
        eng = self.eng
        picks: list[tuple[str, str, dict, float]] = []
        for und in [u.upper() for u in eng.s.options_allowed_underlyings]:
            try:
                chain = await self._calls(und, now.date())
                spot = await eng.broker.last_price(und)
                if not chain or not spot:
                    continue
                for kind, c in eng.pick_target_and_atm(chain, float(spot)):
                    picks.append((und, kind, c, float(spot)))
            except Exception as e:  # noqa: BLE001
                log.debug("spread log %s: %s", und, e)
        if not picks:
            return 0
        syms = list(dict.fromkeys(c["symbol"] for _, _, c, _ in picks))
        snaps: dict = {}
        for i in range(0, len(syms), 20):
            snaps.update(await eng.broker.parsed_option_snapshots(syms[i:i + 20]))
        mins = (now.hour * 60 + now.minute + now.second / 60) - (OPEN.hour * 60 + OPEN.minute)
        ts = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = []
        for und, kind, c, spot in picks:
            d = snaps.get(c["symbol"]) or {}
            bid, ask = d.get("bid"), d.get("ask")
            mid = (bid + ask) / 2 if bid and ask and bid > 0 and ask >= bid else None
            asof = d.get("as_of")
            rows.append({"ts": ts, "minutes_from_open": round(mins, 1), "underlying": und,
                         "symbol": c["symbol"], "kind": kind, "dte": c["dte"], "strike": c["strike"],
                         "bid": bid, "ask": ask, "mid": mid,
                         "spread_pct": round((ask - bid) / mid * 100, 2) if mid else None,
                         "iv": d.get("iv"), "delta": d.get("delta"), "volume": d.get("volume"),
                         "open_interest": d.get("open_interest"), "underlying_price": spot,
                         "quote_age_s": round(max(0.0, time.time() - float(asof)), 1) if asof else None})
        eng.db.add_option_spreads(rows)
        return len(rows)
