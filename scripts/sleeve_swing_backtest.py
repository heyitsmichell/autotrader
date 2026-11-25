#!/usr/bin/env python3
"""Swing sleeve backtest at $500k sizing (multi-strategy plan, step 1).

Runs live variant d (2-ATR stop, 20-day time stop, no TP) with SIZE_FROM_RISK at 0.5% of equity,
MAX_POSITION_USD 10% of the sleeve, and saves the daily equity curve so it can be combined with
the other sleeves. Bars are cut at 2025-11-21 (the locked holdout starts 2025-11-24 and is never loaded).

  python scripts/sleeve_swing_backtest.py
Writes data/backtests/sleeves_2026-11-25/swing_*.json and swing_*_daily.csv.
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import swing_backtest as sb  # noqa: E402

CUTOFF = "2025-11-21"
OUT = ROOT / "data" / "backtests" / "sleeves_2026-11-25"
CASH = 500_000.0
D = sb.V2["d_gb0_plain_sl2_time20"]
SIZE = {"size_from_risk": True, "risk_per_trade_pct": 0.5, "max_position_usd": CASH * 0.10,
        "max_order_usd": CASH * 0.10, "max_order_qty": 1_000_000, "backtest_initial_cash": CASH,
        "backtest_commission_per_order": 0.0}


def cut_bars(src: Path, dst: Path, symbols: list[str]) -> list[str]:
    dst.mkdir(parents=True, exist_ok=True)
    have = []
    for s in symbols:
        f = src / f"{s}.csv"
        if not f.exists():
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        keep = [lines[0]] + [ln for ln in lines[1:] if ln[:10] <= CUTOFF]
        (dst / f"{s}.csv").write_text("\n".join(keep) + "\n", encoding="utf-8")
        have.append(s)
    return have


def run(tag: str, src: Path, tmp: Path, overrides: dict) -> dict:
    from app.backtest.engine import run_backtest
    s0 = sb.build_settings({}, src)
    uni = sb.universe_for(s0)
    have = cut_bars(src, tmp, sorted(set(uni) | {"SPY"}))
    s = sb.build_settings({**D, **SIZE, **overrides}, tmp)
    uni = [u for u in uni if u in have]
    res = run_backtest(s, universe=uni, benchmark_symbol="SPY",
                       sec_fee_rate=2.78e-05, taf_per_share=0.000166)
    n = min(len(res.equity_dates), len(res.equity_curve))
    dates, eq = res.equity_dates[:n], res.equity_curve[:n]
    first = min((t.entry_day for t in res.trades), default=dates[0])
    start = max(0, next((i for i, d in enumerate(dates) if d >= first), 0) - 1)
    m = sb.seg_metrics(dates, eq, res.exposure_curve, res.trades, start, n, CASH)
    b = res.benchmark_equity[:n]
    m["spy_return"] = b[n - 1] / b[start] - 1 if b[start] > 0 else None
    m["universe_n"] = len(uni)
    with open(OUT / f"swing_{tag}_daily.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "equity", "exposure"])
        for i in range(start, n):
            w.writerow([dates[i].isoformat(), round(eq[i], 2),
                        round(res.exposure_curve[i], 4) if i < len(res.exposure_curve) else ""])
    (OUT / f"swing_{tag}.json").write_text(json.dumps(m, indent=1, default=str), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return m


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    scratch = OUT / "_bars"
    jobs = [("live87", ROOT / "data" / "bars_yahoo", {}),
            ("long_ext", ROOT / "data" / "bars_yahoo_ext", {}),
            ("long_ext_risk1", ROOT / "data" / "bars_yahoo_ext", {"risk_per_trade_pct": 1.0})]
    for tag, src, ov in jobs:
        m = run(tag, src, scratch / tag, ov)
        print(tag, {k: m.get(k) for k in ("from", "to", "universe_n", "trades", "cagr", "max_dd", "sharpe",
                                          "exposure", "avg_risk_usd", "avg_position_usd", "spy_return")}, flush=True)
    shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
