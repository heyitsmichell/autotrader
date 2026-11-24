"""Which watchlist/universe symbols can trade in Webull's overnight (NIGHT) session?

READ ONLY. The UAT order preview accepts any symbol in NIGHT (it only estimates cost), so it is not
an eligibility test. Instead this reads the market snapshot: a symbol that trades in Webull's 24/5
overnight session carries an "OVN Price" (overnight print) from the last night session. Symbols
are fetched in batches of 20 per snapshot call.

  .venv\\Scripts\\python.exe scripts\\check_overnight_eligibility.py [--symbols A,B] [--gap 2.5]

Writes data/overnight_eligibility.json: {symbol: {"ok": bool, "ovn_price", "ovn_volume"}}.
Eligible means an overnight print WITH volume in the last night session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.broker import Broker  # noqa: E402
from app.config import load_settings  # noqa: E402



async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--gap", type=float, default=2.5)
    a = ap.parse_args()
    s = load_settings()
    syms = [x.strip().upper() for x in a.symbols.split(",") if x.strip()] or list(s.watchlist)
    broker = Broker(s)
    await broker.start()
    out: dict[str, dict] = {}
    try:
        from app.broker.parsers import parse_snapshot
        for i in range(0, len(syms), 20):
            chunk = syms[i:i + 20]
            text = await broker.call("get_stock_snapshot", symbols=",".join(chunk),
                                    extend_hour_required=True, overnight_required=True)
            snaps = parse_snapshot(text)
            for sym in chunk:
                sn = snaps.get(sym) or {}
                ovn, vol = sn.get("ovn_price"), sn.get("ovn_volume")
                # eligible = it actually traded in the last night session (a price with volume)
                out[sym] = {"ok": bool(ovn) and bool(vol), "ovn_price": ovn, "ovn_volume": vol}
                print(f"{sym}: {'OK' if out[sym]['ok'] else 'NO'} ovn={ovn} vol={vol}", flush=True)
            await asyncio.sleep(a.gap)
    finally:
        await broker.stop()
        (ROOT / "data" / "overnight_eligibility.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
