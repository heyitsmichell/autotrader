#!/usr/bin/env python3
"""Options-entry backtest: buy CALLS on the live strategy's daily signals vs buying the stock.

MODEL, NOT MARKET PRICES: no historical option chains exist, so calls are priced with Black-Scholes
(no dividends, r=4%), IV proxy = max(0.12, 20d realized vol * 1.15), recomputed daily.
Research window 2022-01-01 -> 2025-11-21 (locked holdout untouched).
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import math
import sys
from datetime import date, datetime, timedelta
from itertools import product
from pathlib import Path
from statistics import NormalDist

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "experiments" / "options_entry"
REG = ROOT / "data" / "experiments" / "trials_registry.csv"
START, END = date(2022, 1, 1), date(2025, 11, 21)
R, IV_MULT, IV_FLOOR = 0.04, 1.15, 0.12
ACCOUNT, TRADE_USD = 25000.0, 300.0
COMM = 0.65 / 100.0          # per option, each side
STOCK_SLIP = 0.0005          # 5 bps each side on stock baseline
N = NormalDist()


def bs_call(S, K, T, sigma, r=R):
    if T <= 0:
        return max(S - K, 0.0)
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    return S * N.cdf(d1) - K * math.exp(-r * T) * N.cdf(d1 - sq)


def bs_delta(S, K, T, sigma, r=R):
    sq = sigma * math.sqrt(T)
    return N.cdf((math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq)


def strike_for_delta(S, T, sigma, target, r=R):
    lo, hi = S * 0.3, S * 3.0          # delta decreases in K
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_delta(S, mid, T, sigma, r) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def realized_vol(closes, i, n=20):
    if i < n:
        return None
    seg = np.asarray(closes[i - n:i + 1], float)
    return float(np.std(np.diff(np.log(seg)), ddof=1) * math.sqrt(252))


def iv_proxy(closes, i):
    rv = realized_vol(closes, i)
    return IV_FLOOR if rv is None else max(IV_FLOOR, rv * IV_MULT)


# ---------------------------------------------------------------- signals / stock trades
def load_data(settings):
    from app.backtest.data import load_csv_dir
    from app.backtest.signals import build_features
    syms = list(settings.options_allowed_underlyings)
    bars = {}
    for d in (ROOT / "data" / "bars_yahoo", ROOT / "data" / "bars_yahoo_ext"):
        miss = [s for s in syms + ["SPY"] if s not in bars]
        for s, rows in load_csv_dir(d, miss).items():
            if rows:
                bars[s] = [b for b in rows if b["time"].date() <= END]
    feats = {s: build_features(b) for s, b in bars.items()}
    return syms, bars, feats


def simulate_stock_trades(settings, syms, bars, feats):
    from app.backtest.engine import _align_benchmark
    from app.backtest.signals import signal_at
    trades = []
    sb, sf = bars["SPY"], feats["SPY"]
    for s in syms:
        if s not in bars:
            continue
        b, f = bars[s], feats[s]
        ab, af, first = _align_benchmark(b, sb, sf)
        lb = int(getattr(settings, "relative_strength_lookback", 20))
        i, n = 0, len(b)
        while i < n - 1:
            d = b[i]["time"].date()
            if d < START or i < 60:
                i += 1
                continue
            use = i >= first + max(0, lb)
            r = signal_at(b, f, i, settings, benchmark_bars=ab if use else None,
                          benchmark_features=af if use else None)
            if not r.get("ok"):
                i += 1
                continue
            e = i + 1
            entry = b[e]["open"]
            sl = r["sl"]
            ex_i, ex_px, why = None, None, None
            for j in range(e, n):
                if b[j]["low"] <= sl:
                    ex_i, ex_px, why = j, min(sl, b[j]["open"]), "stop"
                    break
                if j - e >= 20:
                    ex_i, ex_px, why = j, b[j]["close"], "time"
                    break
            if ex_i is None:
                ex_i, ex_px, why = n - 1, b[n - 1]["close"], "end"
            trades.append(dict(sym=s, sig_i=i, entry_i=e, exit_i=ex_i, entry_px=entry, sl=sl,
                               stock_exit_px=ex_px, stock_reason=why))
            i = ex_i + 1   # one position per symbol; next signal after the exit close
    return trades


# ---------------------------------------------------------------- option trade
def run_option(tr, b, delta, dte, mode, h):
    """mode: prem | prem_u | u_stop."""
    closes = [x["close"] for x in b]
    e = tr["entry_i"]
    S0 = tr["entry_px"]
    sig = iv_proxy(closes, tr["sig_i"])
    ed = b[e]["time"].date()
    expiry = ed + timedelta(days=dte)
    T0 = dte / 365.0
    K = strike_for_delta(S0, T0, sig, delta)
    prem0 = bs_call(S0, K, T0, sig)
    cost = prem0 * (1 + h) + COMM
    tp = mode in ("prem", "prem_u")
    use_u = mode in ("prem_u", "u_stop")
    path, n = [], len(b)
    for j in range(e, n):
        dj = b[j]["time"].date()
        dte_left = (expiry - dj).days
        val = bs_call(b[j]["close"], K, max(dte_left / 365.0, 0), iv_proxy(closes, j))
        why = None
        if val <= prem0 * 0.5:
            why = "prem_stop"
        elif tp and val >= prem0 * 1.5:
            why = "prem_tp"
        elif mode != "u_stop" and dte_left <= 2:
            why = "dte2"
        elif use_u and b[j]["close"] <= tr["sl"]:
            why = "u_stop"
        elif use_u and j - e >= 20:
            why = "u_time"
        elif dte_left <= 0:
            why = "expiry"
        elif j == n - 1:
            why = "end"
        ret = (val * (1 - h) - COMM) / cost - 1
        path.append((j, ret))
        if why:
            return dict(ret=ret, exit_i=j, reason=why, path=path, K=K, prem0=prem0, iv=sig)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- portfolio
def portfolio(items, dates):
    """items: list of (mark_dates, mark_rets). Fixed TRADE_USD each, concurrent, cum P&L held after exit."""
    idx = {d: k for k, d in enumerate(dates)}
    pnl = np.zeros(len(dates))
    for dts, rets in items:
        pos = {idx[d]: r for d, r in zip(dts, rets) if d in idx}
        if not pos:
            continue
        k0 = min(pos)
        last = 0.0
        for k in range(k0, len(dates)):
            if k in pos:
                last = pos[k] * TRADE_USD
            pnl[k] += last
    eq = ACCOUNT + pnl
    return eq, np.diff(eq) / eq[:-1]


def metrics(eq, rets, dates):
    yrs = (dates[-1] - dates[0]).days / 365.25
    cagr = (eq[-1] / eq[0]) ** (1 / yrs) - 1
    sd = rets.std(ddof=1)
    sh = float(rets.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    py = {}
    for y in (2022, 2023, 2024, 2025):
        ks = [k for k, d in enumerate(dates) if d.year == y]
        prev = ks[0] - 1 if ks[0] > 0 else 0
        py[y] = float(eq[ks[-1]] / eq[prev] - 1)
    return dict(cagr=float(cagr), sharpe=sh, maxdd=dd, per_year=py, final=float(eq[-1]))


MODES = {"prem": "premium rules only (-50%/+50%/DTE2)",
         "prem_u": "premium rules + underlying stop/time",
         "u_stop": "underlying stop/time + -50% stop, no TP"}


def main():
    from app.config.loader import load_settings
    s = dataclasses.replace(load_settings(), backtest_ml_enabled=False, backtest_live_exits=True,
                            backtest_min_bars=250, sl_atr_mult=2.0, bracket_take_profit=False,
                            profit_giveback_pct=0.0, profit_scale_out_enabled=False,
                            profit_breakeven_after_r=0.0, profit_rsi_exhaustion=0.0,
                            backtest_max_hold_days=20, exit_max_hold_days=20,
                            exit_on_trend_break_enabled=False, backtest_exit_on_trend_break=False)
    syms, bars, feats = load_data(s)
    used = [x for x in syms if x in bars]
    print("universe", len(used), "of", len(syms), "missing", [x for x in syms if x not in bars])
    trades = simulate_stock_trades(s, used, bars, feats)
    print("stock trades", len(trades))
    dates = sorted({b["time"].date() for b in bars["SPY"] if START <= b["time"].date() <= END})

    def stock_ret(t):
        return t["stock_exit_px"] * (1 - STOCK_SLIP) / (t["entry_px"] * (1 + STOCK_SLIP)) - 1

    sitems = []
    for t in trades:
        b = bars[t["sym"]]
        pts = []
        for j in range(t["entry_i"], t["exit_i"] + 1):
            px = t["stock_exit_px"] if j == t["exit_i"] else b[j]["close"]
            pts.append((b[j]["time"].date(), px * (1 - STOCK_SLIP) / (t["entry_px"] * (1 + STOCK_SLIP)) - 1))
        sitems.append(([d for d, _ in pts], [r for _, r in pts]))
    eq, rr = portfolio(sitems, dates)
    stock_all = metrics(eq, rr, dates)
    sr = np.array([stock_ret(t) for t in trades])
    stock_all.update(name="STOCK", n=len(trades), win=float((sr > 0).mean()), mean_ret=float(sr.mean()),
                     median_ret=float(np.median(sr)))

    results, trade_rows = [], []
    for h_rt in (0.03, 0.06):
        h = h_rt / 2
        for delta, dte, mode in product((0.40, 0.55, 0.70), (30, 45), MODES):
            name = f"call_d{int(delta * 100)}_dte{dte}_{mode}" + ("_stress6" if h_rt == 0.06 else "")
            its, rets, srets = [], [], []
            for t in trades:
                b = bars[t["sym"]]
                o = run_option(t, b, delta, dte, mode, h)
                its.append(([b[j]["time"].date() for j, _ in o["path"]], [r for _, r in o["path"]]))
                rets.append(o["ret"])
                srets.append(stock_ret(t))
                if h_rt == 0.03:
                    trade_rows.append(dict(
                        variant=name, sym=t["sym"], entry=b[t["entry_i"]]["time"].date(),
                        exit=b[o["exit_i"]]["time"].date(), entry_px=round(t["entry_px"], 3),
                        strike=round(o["K"], 2), iv=round(o["iv"], 3), prem0=round(o["prem0"], 3),
                        opt_ret=round(o["ret"], 4), opt_reason=o["reason"],
                        stock_ret=round(stock_ret(t), 4), stock_reason=t["stock_reason"]))
            eq, rr = portfolio(its, dates)
            m = metrics(eq, rr, dates)
            ra = np.array(rets)
            m.update(name=name, delta=delta, dte=dte, mode=mode, stress=h_rt == 0.06, n=len(ra),
                     win=float((ra > 0).mean()), mean_ret=float(ra.mean()), median_ret=float(np.median(ra)),
                     stock_mean_same=float(np.mean(srets)))
            results.append(m)

    try:
        existing = sum(1 for _ in open(REG, encoding="utf-8")) - 1
    except OSError:
        existing = 0
    ts = datetime.now().isoformat(timespec="seconds")
    with open(REG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for m in [stock_all] + [r for r in results if not r["stress"]]:
            ch = hashlib.md5(m["name"].encode()).hexdigest()[:10]
            w.writerow([ts, "options_entry", m["name"], ch, f"options_underlyings({len(used)})", START, END,
                        round(m["cagr"], 4), round(m["sharpe"], 3), round(m["maxdd"], 4), m["n"],
                        "BS-modelled (r=4%, IV=1.15x RV20); $300/trade 25k acct; turnover col = n trades"])
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "trades.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(trade_rows[0]))
        w.writeheader()
        w.writerows(trade_rows)
    json.dump(dict(stock=stock_all, variants=results, registry_rows_before=existing, universe=used),
              open(OUT / "summary.json", "w"), indent=1, default=str)
    write_report(stock_all, results, existing, used)
    print("done; best:", max((r for r in results if not r["stress"]), key=lambda r: r["sharpe"])["name"])


def write_report(stock, results, existing, used):
    main_r = [r for r in results if not r["stress"]]
    st = {r["name"]: r for r in results if r["stress"]}
    top = sorted(main_r, key=lambda r: -r["sharpe"])
    best = top[0]
    beats = [r for r in main_r if r["sharpe"] > stock["sharpe"] and r["cagr"] > stock["cagr"]]

    def f(x):
        return f"{x * 100:.1f}%"

    L = ["# Options-entry backtest: calls on the live signals vs the stock", "",
         "## Verdict",
         "1. This is a Black-Scholes MODEL (no real option chains), so treat it as a rough guide only.",
         f"2. Best variant by Sharpe: `{best['name']}` Sharpe {best['sharpe']:.2f}, CAGR {f(best['cagr'])}, maxDD {f(best['maxdd'])}; stock (same signals, $300/trade) Sharpe {stock['sharpe']:.2f}, CAGR {f(stock['cagr'])}, maxDD {f(stock['maxdd'])}.",
         f"3. {len(beats)} of 18 variants beat the stock on BOTH Sharpe and CAGR at the same $300 per trade (options carry far more leverage per dollar, so this is not risk-equal).",
         f"4. Mean return per trade on premium: best {f(best['mean_ret'])} vs {f(best['stock_mean_same'])} for the stock on the same trades; median {f(best['median_ret'])}.",
         f"5. {existing} trials were already in the registry and 18 more were tried here, so any apparent edge needs a deflated-Sharpe haircut; do not enable automated options entry on this evidence alone.",
         "", f"Universe: {len(used)} symbols ({', '.join(used)}); {stock['n']} signals/trades, 2022-01-01 to 2025-11-21.", "",
         "## All variants (3% round-trip spread; stress = 6%)", "",
         "| variant | n | win | mean ret | median ret | stock mean (same trades) | CAGR | Sharpe | maxDD | CAGR @6% | Sharpe @6% |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    L.append(f"| STOCK $300/trade | {stock['n']} | {f(stock['win'])} | {f(stock['mean_ret'])} | {f(stock['median_ret'])} | - | {f(stock['cagr'])} | {stock['sharpe']:.2f} | {f(stock['maxdd'])} | - | - |")
    for r in main_r:
        s6 = st[r["name"] + "_stress6"]
        L.append(f"| {r['name']} | {r['n']} | {f(r['win'])} | {f(r['mean_ret'])} | {f(r['median_ret'])} | {f(r['stock_mean_same'])} | {f(r['cagr'])} | {r['sharpe']:.2f} | {f(r['maxdd'])} | {f(s6['cagr'])} | {s6['sharpe']:.2f} |")
    L += ["", "## Per-year returns (best 3 by Sharpe + stock)", "",
          "| variant | 2022 | 2023 | 2024 | 2025 |", "|---|---|---|---|---|"]
    for r in [stock] + top[:3]:
        L.append(f"| {r['name']} | " + " | ".join(f(r['per_year'][y]) for y in (2022, 2023, 2024, 2025)) + " |")
    L += ["", "## Assumptions", "",
          "- Options are MODELLED: Black-Scholes, no dividends, r=4% flat. Not market prices; real skew, term structure, bid/ask and earnings IV jumps/crush are absent.",
          "- IV proxy = max(0.12, 20-day realized vol x 1.15), recomputed daily at each close. Entry IV uses the signal-day close vol; entry spot = next open.",
          "- Strike solved so BS delta = target; expiry = entry + target DTE calendar days.",
          "- Signals: live `signal_at` (v2 consensus gate, ML off, SPY aligned by date), enter next open, one position per symbol; stock exits = variant d (2-ATR stop intraday, 20-trading-day time stop at close, no TP).",
          "- Option exits checked at daily closes only: premium <= -50%; premium >= +50% (modes 1-2); DTE <= 2 (modes 1-2); underlying close <= signal stop and 20-day time stop (modes 2-3); forced close at data end.",
          "- Costs: 1.5% of premium each side (3% round trip; 6% stress also shown) + $0.65/contract each side. Stock baseline: 5 bps slippage each side, no commission.",
          "- Portfolio: fixed $300 premium (or stock) per trade, fractional contracts allowed (unrealistic: real contracts cost hundreds of dollars), $25,000 account, concurrent positions, daily marked P&L, no compounding. Option capital at risk is smaller than stock notional, so CAGR comparisons are NOT leverage-equal.",
          f"- Multiple testing: {existing} rows in trials_registry.csv before this run; 19 rows (18 variants + stock) appended with lab=options_entry (turnover column = trade count). Picking the best of 18 inflates Sharpe.",
          "- 2022-2025 is mostly one regime; the locked holdout (after 2025-11-23) was NOT used."]
    (OUT / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
