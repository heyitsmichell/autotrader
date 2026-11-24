"""Fetch intraday bars (M5/M15) from Webull via the MCP transport, paging back with end_time.

Writes data/bars_intraday/<timespan>/<SYMBOL>.csv (ts_utc,open,high,low,close,volume),
regular session only (13:30-20:00 UTC in EDT / 14:30-21:00 in EST), de-duplicated, ascending.
Resumable: symbols with a ".done" marker are skipped. Market data only.

  python scripts/fetch_intraday_bars.py --timespan M15 [--symbols A,B] [--days 730]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.broker.parsers import parse_bars  # noqa: E402
from app.broker.transport import McpTransport  # noqa: E402
from app.config.loader import load_settings  # noqa: E402

ET = ZoneInfo("America/New_York")
GAP = 2.0  # seconds between MCP calls
MAX_COUNT = 1200


def regular(t: datetime) -> bool:
    e = t.astimezone(ET)
    m = e.hour * 60 + e.minute
    return e.weekday() < 5 and 570 <= m < 960


async def fetch_symbol(tr, sym: str, ts: str, days: int) -> dict[datetime, dict]:
    floor = datetime.now(timezone.utc) - timedelta(days=days)
    got: dict[datetime, dict] = {}
    end = None
    while True:
        kw = dict(symbols=sym, timespan=ts, count=MAX_COUNT)
        if end:
            kw["end_time"] = end
        try:
            bars = parse_bars(await tr.call("get_stock_bars", **kw))
        except Exception as e:
            if got and "180 day" in str(e):  # Webull history limit reached
                break
            raise
        await asyncio.sleep(GAP)
        new = [b for b in bars if b["time"] not in got]
        if not new:
            break
        for b in new:
            got[b["time"]] = b
        oldest = min(got)
        if oldest < floor:
            break
        end = int(oldest.timestamp() * 1000) - 1
    return got


async def main() -> None:
    global GAP
    ap = argparse.ArgumentParser()
    ap.add_argument("--timespan", required=True, choices=["M5", "M15"])
    ap.add_argument("--symbols", default="")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--gap", type=float, default=2.0, help="seconds between MCP calls")
    ap.add_argument("--pause", type=float, default=180.0, help="seconds to wait after a rate limit")
    a = ap.parse_args()
    GAP = a.gap
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] or \
        sorted(p.stem for p in (ROOT / "data" / "bars_yahoo").glob("*.csv"))
    out = ROOT / "data" / "bars_intraday" / a.timespan
    out.mkdir(parents=True, exist_ok=True)
    tr = McpTransport(load_settings())
    await tr.start()
    t0 = time.time()
    try:
        for i, sym in enumerate(syms, 1):
            f, done = out / f"{sym}.csv", out / f"{sym}.done"
            if done.exists():
                print(f"[{i}/{len(syms)}] {sym} skip", flush=True)
                continue
            got, stop = None, False
            for attempt in range(1, 4):
                try:
                    got = await fetch_symbol(tr, sym, a.timespan, a.days)
                    break
                except Exception as e:
                    print(f"[{i}/{len(syms)}] {sym} ERROR {str(e)[:150]}", flush=True)
                    if "429" in str(e) or "rate" in str(e).lower():
                        if attempt == 3:
                            print("still rate limited after 3 pauses; stopping", flush=True)
                            stop = True
                        else:
                            print(f"rate limited; pausing {a.pause:g}s (attempt {attempt}/3)", flush=True)
                            await asyncio.sleep(a.pause)
                        continue
                    break
            if stop:
                break
            if got is None:
                continue
            rows = [got[t] for t in sorted(got) if regular(t)]
            with open(f, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["ts_utc", "open", "high", "low", "close", "volume"])
                for b in rows:
                    w.writerow([b["time"].strftime("%Y-%m-%dT%H:%M:%SZ"), b["open"], b["high"],
                                b["low"], b["close"], b["volume"]])
            done.write_text("ok")
            span = f"{rows[0]['time']:%Y-%m-%d}..{rows[-1]['time']:%Y-%m-%d}" if rows else "empty"
            print(f"[{i}/{len(syms)}] {sym} {len(rows)} bars {span} ({time.time()-t0:.0f}s)", flush=True)
    finally:
        await tr.stop()


if __name__ == "__main__":
    asyncio.run(main())
