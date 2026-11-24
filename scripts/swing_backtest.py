#!/usr/bin/env python3
"""Swing-strategy backtest matrix on the live universe (tech_semis + WATCHLIST + SPY).

Reads bars from data/bars_yahoo (see scripts/fetch_yahoo_bars.py), settings from .env (read only),
applies per-variant overrides, models the LIVE exit stack (BACKTEST_LIVE_EXITS) and writes one JSON
per variant plus summary.json / summary.md under data/backtests/swing_<tag>/.

  .venv/Scripts/python.exe scripts/swing_backtest.py --stage exits
  .venv/Scripts/python.exe scripts/swing_backtest.py --only baseline,tp3 --workers 4

In-sample / out-of-sample: nothing is fitted, so one run over the whole history is sliced:
the last --oos-days calendar days are the held-out period (metrics from the equity curve and from
trades by exit date).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import time
from datetime import timedelta
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT_ROOT = ROOT / "data" / "backtests"

# --- variants -------------------------------------------------------------------------------
# Overrides are Settings attribute names. "baseline" = the live .env as it is today.
EXIT_BASE = {}
NOTP = {"bracket_take_profit": False}
V: dict[str, dict] = {"baseline": {}}
for m in (3.0, 4.0, 6.0):
    V[f"tp{m:g}"] = {"tp_atr_mult": m}
V["notp"] = dict(NOTP)
V["notp_be1"] = {**NOTP, "profit_breakeven_after_r": 1.0}
for m in (1.5, 2.5, 3.0):
    V[f"notp_be1_trail{m:g}"] = {**NOTP, "profit_breakeven_after_r": 1.0, "trailing_stop_enabled": True,
                                  "trailing_stop_atr_mult": m, "trailing_stop_activation_r": 1.0}
for m in (1.5, 2.5, 3.0):
    V[f"tp3_be1_trail{m:g}"] = {"tp_atr_mult": 3.0, "profit_breakeven_after_r": 1.0,
                                 "trailing_stop_enabled": True, "trailing_stop_atr_mult": m,
                                 "trailing_stop_activation_r": 1.0}
# the live give-back rule (20% of peak gain, from +1%) and RSI exhaustion cut winners hard
for gb in (35.0, 50.0, 0.0):
    V[f"notp_be1_trail2.5_gb{gb:g}"] = {**NOTP, "profit_breakeven_after_r": 1.0, "trailing_stop_enabled": True,
                                         "trailing_stop_atr_mult": 2.5, "profit_giveback_pct": gb}
V["notp_be1_trail2.5_gb0_norsi"] = {**NOTP, "profit_breakeven_after_r": 1.0, "trailing_stop_enabled": True,
                                     "trailing_stop_atr_mult": 2.5, "profit_giveback_pct": 0.0,
                                     "profit_rsi_exhaustion": 0.0}
V["notp_noscale_trail2.5_gb0"] = {**NOTP, "profit_scale_out_enabled": False, "profit_breakeven_after_r": 1.0,
                                   "trailing_stop_enabled": True, "trailing_stop_atr_mult": 2.5,
                                   "profit_giveback_pct": 0.0}
V["notp_noscale_trail3_gb0_norsi"] = {**NOTP, "profit_scale_out_enabled": False, "profit_breakeven_after_r": 1.0,
                                       "trailing_stop_enabled": True, "trailing_stop_atr_mult": 3.0,
                                       "profit_giveback_pct": 0.0, "profit_rsi_exhaustion": 0.0}
V["baseline_gb0"] = {"profit_giveback_pct": 0.0}
V["baseline_gb35"] = {"profit_giveback_pct": 35.0}
ZC = {"backtest_commission_per_order": 0.0, "backtest_slippage_bps": 0.0, "backtest_atr_slippage_mult": 0.0,
      "backtest_impact_bps": 0.0}
V["baseline_zerocost"] = dict(ZC)
V["baseline_gb0_zerocost"] = {**ZC, "profit_giveback_pct": 0.0}
V["baseline_nocomm"] = {"backtest_commission_per_order": 0.0}
V["plain_sl2_time20_zerocost"] = {**ZC, **NOTP, "profit_scale_out_enabled": False, "profit_breakeven_after_r": 0.0,
                                  "profit_giveback_pct": 0.0, "profit_rsi_exhaustion": 0.0}
V["plain_sl2_time20"] = {**NOTP, "profit_scale_out_enabled": False, "profit_breakeven_after_r": 0.0,
                         "profit_giveback_pct": 0.0, "profit_rsi_exhaustion": 0.0}
V["baseline_ta015"] = {"consensus_ta_min": 0.15}   # entry-filter sensitivity (live default threshold)


# v2 matrix (run with --stage v2): live-exit stack, give-back off, then exit-style alternatives
GB0 = {"profit_giveback_pct": 0.0}
V2: dict[str, dict] = {
    "a_baseline": {},
    "b_gb0": dict(GB0),
    "c_gb0_notp_trail": {**GB0, **NOTP, "trailing_stop_enabled": True, "trailing_stop_atr_mult": 2.5,
                         "trailing_stop_activation_r": 1.0},
    "d_gb0_plain_sl2_time20": {**GB0, **NOTP, "profit_scale_out_enabled": False, "profit_breakeven_after_r": 0.0,
                               "profit_rsi_exhaustion": 0.0, "sl_atr_mult": 2.0, "backtest_max_hold_days": 20},
    "e_gb0_cooldown3": {**GB0, "_cooldown": 3},
}


def sizing(name: str, base: dict, **kw) -> None:
    V[name] = {**base, **kw}


# stage 2 variants are added by --stage sizing / the "BEST" env var (see main)
STAGES = {
    "exits": [k for k in V],
}


def sizing_variants(best: dict, tag: str) -> dict[str, dict]:
    out = {}
    wide = {"max_order_usd": 50000.0, "max_position_usd": 5000.0, "max_order_qty": 1000}
    for label, base in (("base", {}), (tag, best)):
        out[f"{label}_size_cur"] = dict(base)          # live caps: $500 order, $200 risk
        for cash in (25000.0, 50000.0):
            c = f"{int(cash / 1000)}k"
            out[f"{label}_riskusd200_{c}"] = {**base, **wide, "size_from_risk": True,
                                               "backtest_initial_cash": cash}
            for pct in (1.0, 2.0):
                out[f"{label}_risk{pct:g}pct_{c}"] = {**base, **wide, "size_from_risk": True,
                                                       "risk_per_trade_pct": pct,
                                                       "max_position_usd": cash * 0.25,
                                                       "backtest_initial_cash": cash}
    return out


# --- run + metrics --------------------------------------------------------------------------
def build_settings(overrides: dict, data_dir: Path):
    from app.config import load_settings
    s = load_settings()
    base = {
        "backtest_data_dir": data_dir, "backtest_results_dir": OUT_ROOT / "_scratch",
        "backtest_live_exits": True, "backtest_ml_enabled": False, "backtest_min_bars": 250,
        "backtest_initial_cash": 25000.0, "backtest_max_gross_exposure_pct": 100.0,
    }
    return dataclasses.replace(s, **{**base, **overrides})


def universe_for(settings) -> list[str]:
    from app.universe import get_predefined_universe
    u = get_predefined_universe("tech_semis") + [x.upper() for x in settings.watchlist]
    return list(dict.fromkeys(u))


def seg_metrics(dates, equity, exposure, trades, start_idx: int, end_idx: int, cash0: float) -> dict:
    eq = equity[start_idx:end_idx]
    d = dates[start_idx:end_idx]
    if len(eq) < 3:
        return {}
    d0, d1 = d[0], d[-1]
    tr = [t for t in trades if t.exit_day and d0 <= t.exit_day <= d1]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1))
    years = max((d1 - d0).days / 365.25, 1e-9)
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    tot = eq[-1] / eq[0] - 1
    wins = [t for t in tr if t.pnl > 0]
    losses = [t for t in tr if t.pnl <= 0]
    streak = cur = 0
    for t in sorted(tr, key=lambda x: (x.exit_day, x.symbol)):
        cur = cur + 1 if t.pnl <= 0 else 0
        streak = max(streak, cur)
    gp, gl = sum(t.pnl for t in wins), -sum(t.pnl for t in losses)
    exp_slice = exposure[start_idx - 1:end_idx - 1] if exposure else []
    rs = [t.r_multiple for t in tr if t.risk_usd > 0]
    return {
        "from": str(d0), "to": str(d1), "trades": len(tr),
        "win_rate": len(wins) / len(tr) if tr else 0.0,
        "avg_win_R": sum(t.r_multiple for t in wins if t.risk_usd > 0) / max(1, sum(1 for t in wins if t.risk_usd > 0)),
        "avg_loss_R": sum(t.r_multiple for t in losses if t.risk_usd > 0) / max(1, sum(1 for t in losses if t.risk_usd > 0)),
        "expectancy_R": sum(rs) / len(rs) if rs else 0.0,
        "profit_factor": gp / gl if gl > 0 else float("inf") if gp > 0 else 0.0,
        "total_return": tot, "cagr": (1 + tot) ** (1 / years) - 1 if tot > -1 else -1.0,
        "max_dd": mdd, "sharpe": (mean / sd * math.sqrt(252)) if sd > 0 else 0.0,
        "exposure": sum(exp_slice) / len(exp_slice) if exp_slice else 0.0,
        "avg_hold_days": sum(t.hold_days for t in tr) / len(tr) if tr else 0.0,
        "worst_losing_streak": streak,
        "avg_risk_usd": sum(t.risk_usd for t in tr) / len(tr) if tr else 0.0,
        "avg_position_usd": sum(t.quantity * t.entry_fill_price for t in tr) / len(tr) if tr else 0.0,
        "worst_trade_R": min(rs) if rs else 0.0,
        "exit_reasons": {k: [sum(1 for t in tr if t.exit_reason == k),
                             round(sum(t.r_multiple for t in tr if t.exit_reason == k and t.risk_usd > 0)
                                   / max(1, sum(1 for t in tr if t.exit_reason == k and t.risk_usd > 0)), 3)]
                         for k in sorted({t.exit_reason for t in tr})},
    }


def run_variant(args) -> dict:
    name, overrides, data_dir, oos_days, fees, cooldown = args
    overrides = dict(overrides)
    cooldown = int(overrides.pop("_cooldown", cooldown))
    os.chdir(ROOT)
    from app.backtest.engine import run_backtest
    t0 = time.time()
    s = build_settings(overrides, Path(data_dir))
    res = run_backtest(s, universe=universe_for(s), benchmark_symbol="SPY",
                       reentry_cooldown_days=cooldown, **fees)
    dates, eq = res.equity_dates, res.equity_curve
    n = min(len(dates), len(eq))
    dates, eq = dates[:n], eq[:n]
    # trading starts once 250 bars exist; start the equity slice at the first non-flat/first trade day
    first_trade = min((t.entry_day for t in res.trades), default=dates[0])
    start = max(0, next((i for i, d in enumerate(dates) if d >= first_trade), 0) - 1)
    cut = dates[-1] - timedelta(days=oos_days)
    cut_i = next((i for i, d in enumerate(dates) if d >= cut), n - 1)
    cash0 = s.backtest_initial_cash
    out = {
        "name": name, "overrides": {k: str(v) for k, v in overrides.items()}, "cooldown": cooldown, "fees": fees,
        "universe_n": len(res.universe), "seconds": round(time.time() - t0, 1),
        "full": seg_metrics(dates, eq, res.exposure_curve, res.trades, start, n, cash0),
        "in_sample": seg_metrics(dates, eq, res.exposure_curve, res.trades, start, cut_i + 1, cash0),
        "oos": seg_metrics(dates, eq, res.exposure_curve, res.trades, cut_i, n, cash0),
    }
    # SPY buy-and-hold over the same windows
    b = res.benchmark_equity[:n]
    for key, (a, z) in (("full", (start, n)), ("in_sample", (start, cut_i + 1)), ("oos", (cut_i, n))):
        if out[key] and z - a > 2 and b[a] > 0:
            out[key]["spy_return"] = b[z - 1] / b[a] - 1
    return out


def md_table(rows: list[dict], seg: str) -> str:
    hdr = ("| variant | trades | win% | avgW R | avgL R | exp R | PF | return | CAGR | maxDD | Sharpe | expo | hold d | streak |\n"
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    lines = []
    for r in rows:
        m = r.get(seg) or {}
        if not m:
            continue
        pf = m["profit_factor"]
        lines.append(f"| {r['name']} | {m['trades']} | {m['win_rate']*100:.0f} | {m['avg_win_R']:.2f} | {m['avg_loss_R']:.2f} | "
                     f"{m['expectancy_R']:+.3f} | {'inf' if pf == float('inf') else f'{pf:.2f}'} | {m['total_return']*100:+.1f}% | "
                     f"{m['cagr']*100:+.1f}% | {m['max_dd']*100:.1f}% | {m['sharpe']:.2f} | {m['exposure']*100:.0f}% | "
                     f"{m['avg_hold_days']:.1f} | {m['worst_losing_streak']} |")
    return hdr + "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma list of variant names")
    ap.add_argument("--stage", default="exits", choices=["exits", "sizing", "v2"])
    ap.add_argument("--best", default="", help="sizing stage: variant name to size on top of baseline")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--oos-days", type=int, default=365)
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "bars_yahoo"))
    ap.add_argument("--tag", default="2026-11-24")
    ap.add_argument("--commission", type=float, default=None, help="override BACKTEST_COMMISSION_PER_ORDER (Webull: 0)")
    ap.add_argument("--slippage-bps", type=float, default=None, help="override BACKTEST_SLIPPAGE_BPS")
    ap.add_argument("--sec-fee", type=float, default=0.0, help="SEC fee rate on sell notional (0.0000278)")
    ap.add_argument("--taf", type=float, default=0.0, help="FINRA TAF $/share on sells (0.000166, max $8.30)")
    ap.add_argument("--cooldown", type=int, default=0, help="re-entry cooldown (trading days) for every variant")
    ap.add_argument("--costs", default="env", choices=["env", "nocomm"],
                    help="env = BACKTEST_* from .env ($1/order, 5bps slip, 5bps impact); "
                         "nocomm = same but $0 commission (Webull US stocks are commission-free)")
    args = ap.parse_args()
    out_dir = OUT_ROOT / f"swing_{args.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    variants = dict(V)
    if args.stage == "sizing":
        best = V[args.best] if args.best else {}
        variants = sizing_variants(best, args.best or "best")
    if args.stage == "v2":
        variants = dict(V2)
    if args.only:
        variants = {k: variants[k] for k in args.only.split(",")}
    if args.costs == "nocomm":
        variants = {k: {"backtest_commission_per_order": 0.0, **v} for k, v in variants.items()}
    glob_over = {}
    if args.commission is not None:
        glob_over["backtest_commission_per_order"] = args.commission
    if args.slippage_bps is not None:
        glob_over["backtest_slippage_bps"] = args.slippage_bps
    fees = {"sec_fee_rate": args.sec_fee, "taf_per_share": args.taf}
    jobs = [(k, {**glob_over, **v}, args.data_dir, args.oos_days, fees, args.cooldown)
            for k, v in variants.items()]
    suffix = "" if args.costs == "env" else "_nocomm"
    out_dir = OUT_ROOT / f"swing_{args.tag}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    pool = Pool(args.workers) if args.workers > 1 and len(jobs) > 1 else None
    results = []
    if True:
        for r in (pool.imap_unordered(run_variant, jobs) if pool else map(run_variant, jobs)):
            (out_dir / f"{r['name']}.json").write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
            f = r["full"]
            print(f"{r['name']:<34} {r['seconds']:>5}s  trades={f.get('trades')} exp={f.get('expectancy_R', 0):+.3f}R "
                  f"ret={f.get('total_return', 0)*100:+.1f}% dd={f.get('max_dd', 0)*100:.1f}%", flush=True)
            results.append(r)
    if pool:
        pool.close()
    order = {k: i for i, k in enumerate(variants)}
    results.sort(key=lambda r: order[r["name"]])
    (out_dir / f"summary_{args.stage}.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    md = "\n\n".join(f"### {seg}\n\n{md_table(results, seg)}" for seg in ("full", "in_sample", "oos"))
    (out_dir / f"summary_{args.stage}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
