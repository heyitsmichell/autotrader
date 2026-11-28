#!/usr/bin/env python3
"""Swing baseline study: does live variant d have an edge over RANDOM entries / EW buy-and-hold / SPY?
Same data, costs, sizing and holdout cut (2025-11-21) as swing_universe_study.py.
   python scripts/swing_baseline_study.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import sys
import time
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import swing_backtest as sb  # noqa: E402
from sleeve_swing_backtest import CASH, CUTOFF, D, SIZE, cut_bars  # noqa: E402
from swing_universe_study import boot_ci, sharpe  # noqa: E402
from swing_universe_study import universes as study_universes  # noqa: E402

EXT = ROOT / "data" / "bars_yahoo_ext"
Y87 = ROOT / "data" / "bars_yahoo"
OUT = ROOT / "data" / "backtests" / "swing_baseline_2026-11-28"
SCR = {"U1": OUT / "_bars_U1", "U2": OUT / "_bars_U2"}
N_SEEDS, WORKERS, TARGET_TRADES = 50, 3, 5880
WIN_U1 = {"full_2011_2025": "2011-01-03", "last3y_2022-11-21_2025-11-21": "2022-11-21"}
EXISTING = {"trades": 5880, "cagr": -0.006, "sharpe": 0.07, "max_dd": 0.546}


def universes():
    have = {p.stem for p in EXT.glob("*.csv") if not p.name.startswith("sp500_list")}
    u1 = [u for u in sb.universe_for(sb.build_settings({}, EXT)) if u in have]
    u2 = sb.universe_for(sb.build_settings({}, Y87))
    return u1, u2


def u_rng(seed, symbol, day, tag):
    h = hashlib.md5(f"{seed}|{symbol}|{day}|{tag}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2.0 ** 64


def make_random_fn(seed, p):
    from app.backtest.signals import signal_at
    from app import ta

    def fn(symbol, bars, features, index, settings, **kw):
        if index < 0 or index >= len(bars):
            return {"ok": False, "reason": "no bar"}
        day = bars[index].get("date") or bars[index].get("ts")
        if u_rng(seed, symbol, day, "p") >= p:
            return {"ok": False, "reason": "random: no entry"}
        real = signal_at(bars, features, index, settings, **kw)
        if not real.get("eligible") or "signal" not in real:
            return real                                  # not eligible / no bar: skipped exactly as real
        sig = dict(real["signal"])
        sig["ta_score"] = u_rng(seed, symbol, day, "score")
        close, atr = sig.get("close"), sig.get("atr")
        if not close or not atr:
            return {"ok": False, "reason": "no atr"}
        sl, tp = ta.levels(close, atr, settings)         # same 2xATR stop (clamped) as live variant d
        if sl is None:
            return {"ok": False, "reason": "no sl"}
        return {"ok": True, "eligible": True, "reason": "", "signal": sig, "gate": real.get("gate"),
                "sl": sl, "tp": tp}
    return fn


def seg(dates, eq, expo, trades, d0):
    n = len(eq)
    i0 = max(1, next(i for i, d in enumerate(dates) if d >= d0))
    m = sb.seg_metrics(dates, eq, expo, trades, i0, n, CASH)
    m["trades_per_day"] = m["trades"] / max(1, n - i0 - 1)
    return m


def run_job(args):
    kind, uni_key, uni, seed, p, starts = args
    from app.backtest.engine import run_backtest
    t0 = time.time()
    s = sb.build_settings({**D, **SIZE}, SCR[uni_key])
    fn = make_random_fn(seed, p) if kind == "RANDOM" else None
    res = run_backtest(s, universe=uni, benchmark_symbol="SPY", sec_fee_rate=2.78e-05, taf_per_share=0.000166,
                       signal_fn=fn)
    n = min(len(res.equity_dates), len(res.equity_curve))
    dates, eq, expo = list(res.equity_dates[:n]), list(res.equity_curve[:n]), list(res.exposure_curve)
    out = {"kind": kind, "uni": uni_key, "seed": seed, "p": p, "dates": dates, "eq": eq,
           "seconds": round(time.time() - t0, 1)}
    if kind == "SWING":
        out["expo"], out["trades"] = expo, res.trades
        out["first_entry"] = min((t.entry_day for t in res.trades), default=dates[0])
    out["wins"] = {k: seg(dates, eq, expo, res.trades, d0) for k, d0 in (starts or {}).items()}
    out["all_trades"] = len([t for t in res.trades if t.exit_day])
    return out


def ew_bh(dates, syms, scr):
    """Equal-weight, monthly rebalance (first trading day, at close). Sell-side SEC fee + TAF applied."""
    px = {s: pd.read_csv(scr / f"{s}.csv", usecols=["date", "close"], index_col=0, parse_dates=True)["close"] for s in syms}
    raw = pd.DataFrame({k: v[~v.index.duplicated()] for k, v in px.items()}).sort_index()
    idx = pd.to_datetime(list(dates))
    has = raw.reindex(idx).notna()
    close = raw.reindex(idx.union(raw.index)).ffill().reindex(idx)
    shares = pd.Series(0.0, index=syms)
    cash, eqv, last_month = CASH, [], None
    for t in idx:
        c = close.loc[t]
        ok = c.notna()
        cf = c.fillna(0.0)
        v = cash + float((shares * cf).sum())
        if (t.year, t.month) != last_month:
            last_month = (t.year, t.month)
            elig = [s for s in syms if has.loc[t, s] and ok[s]]
            if elig:
                new_sh = pd.Series(0.0, index=syms)
                new_sh[elig] = (v / len(elig)) / c[elig]
                new_sh[~ok] = shares[~ok]                       # symbols without a price keep their shares
                sells = (shares - new_sh).clip(lower=0)
                proceeds = float((sells * cf).sum())
                fee = 2.78e-05 * proceeds + sum(min(0.000166 * q, 8.30) for q in sells if q > 0)
                shares = new_sh
                cash = v - float((shares * cf).sum()) - fee
                v = cash + float((shares * cf).sum())
        eqv.append(v)
    return pd.Series(eqv, index=idx)


def metrics_series(s: pd.Series, d0):
    s = s[s.index >= pd.Timestamp(d0)]
    r = s.pct_change().dropna().values
    yrs = max((s.index[-1] - s.index[0]).days / 365.25, 1e-9)
    tot = s.iloc[-1] / s.iloc[0] - 1
    dd = float(((s.cummax() - s) / s.cummax()).max())
    return {"from": str(s.index[0].date()), "to": str(s.index[-1].date()), "cagr": float((1 + tot) ** (1 / yrs) - 1),
            "sharpe": sharpe(r), "max_dd": dd, "total_return": float(tot), "trades": None, "exposure": 1.0}


def pct(a, q):
    return float(np.percentile(a, q))


def main() -> int:
    t_start = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    u1, u2 = universes()
    # the existing study cut ALL its candidate symbols (A+B+CD+SPY) into the scratch dir; the engine result depends on
    # what is in the bars dir, so reproduce exactly that directory content
    SU = study_universes()
    assert SU["A"] == u1
    cut_bars(EXT, SCR["U1"], sorted(set(SU["CD"]) | set(SU["A"]) | set(SU["B"]) | {"SPY"}))
    cut_bars(Y87, SCR["U2"], sorted(set(u2) | {"SPY"}))
    U = {"U1": u1, "U2": u2}
    log = {"universe_sizes": {k: len(v) for k, v in U.items()}}
    pool = Pool(WORKERS, maxtasksperchild=1)
    try:
        sw = {r["uni"]: r for r in pool.map(run_job, [("SWING", "U1", u1, 0, 0, None), ("SWING", "U2", u2, 0, 0, None)])}
        r = sw["U1"]
        dts = list(r["dates"])
        old = seg(dts, r["eq"], r["expo"], r["trades"], datetime.fromisoformat("2011-01-03").date())
        log["sanity"] = {"got": {k: old[k] for k in ("trades", "cagr", "sharpe", "max_dd", "from", "to")}, "expected": EXISTING}
        ok = (old["trades"] == 5880 and abs(old["cagr"] - EXISTING["cagr"]) < 0.0005 + 1e-9
              and abs(old["sharpe"] - EXISTING["sharpe"]) < 0.005 + 1e-9 and abs(old["max_dd"] - EXISTING["max_dd"]) < 0.0005 + 1e-9)
        log["sanity"]["reproduced"] = bool(ok)
        print("SANITY", json.dumps(log["sanity"], default=str), flush=True)
        if not ok:
            (OUT / "sanity_failed.json").write_text(json.dumps(log, indent=1, default=str))
            print("SANITY CHECK FAILED - stopping", flush=True)
            return 2

        W = {}
        dates = list(sw["U1"]["dates"])
        for wn, w0 in WIN_U1.items():
            d0 = datetime.fromisoformat(w0).date()
            fe = min((t.entry_day for t in sw["U1"]["trades"] if t.entry_day >= d0), default=d0)
            i = max(0, next(i for i, d in enumerate(dates) if d >= fe) - 1)
            W[("U1", wn)] = max(d0, dates[i])
        dts2 = list(sw["U2"]["dates"])
        fe2 = sw["U2"]["first_entry"]
        W[("U2", "u2_first_trade_2025-11-21")] = dts2[max(0, next(i for i, d in enumerate(dts2) if d >= fe2) - 1)]
        log["window_starts"] = {f"{k[0]}|{k[1]}": str(v) for k, v in W.items()}

        def starts_for(u):
            return {k[1]: v for k, v in W.items() if k[0] == u}

        # pilot calibration of p on U1 (seed 0, not in 1..50)
        pil, pgrid, best = [], [0.01, 0.03, 0.10], None
        for rnd in range(4):
            res = pool.map(run_job, [("RANDOM", "U1", u1, 0, q, None) for q in pgrid])
            for q in res:
                pil.append({"p": q["p"], "trades_full": q["all_trades"], "seconds": q["seconds"]})
            print("PILOT", pil[-len(pgrid):], flush=True)
            inwin = [x for x in pil if abs(x["trades_full"] / TARGET_TRADES - 1) <= 0.15]
            if inwin:
                best = min(inwin, key=lambda x: abs(x["trades_full"] / TARGET_TRADES - 1))["p"]
                break
            pts = sorted((x["p"], x["trades_full"]) for x in pil)
            lo = [a for a in pts if a[1] < TARGET_TRADES]
            hi = [a for a in pts if a[1] >= TARGET_TRADES]
            if lo and hi:
                a, b = lo[-1], hi[0]
                fr = (math.log(TARGET_TRADES) - math.log(max(a[1], 1))) / (math.log(b[1]) - math.log(max(a[1], 1)) + 1e-12)
                pc = math.exp(math.log(a[0]) + fr * (math.log(b[0]) - math.log(a[0])))
            elif lo:
                pc = min(1.0, lo[-1][0] * 3)
            else:
                pc = hi[0][0] / 3
            pgrid = [pc * 0.9, pc, pc * 1.1]
        if best is None:
            print("COULD NOT CALIBRATE p", pil, flush=True)
            (OUT / "calibration_failed.json").write_text(json.dumps(pil, indent=1))
            return 3
        p = best
        log["pilot"], log["p"] = pil, p

        jobs = [("RANDOM", u, U[u], sd, p, starts_for(u)) for u in ("U1", "U2") for sd in range(1, N_SEEDS + 1)]
        rr = pool.map(run_job, jobs, chunksize=1)
    finally:
        pool.close()
        pool.join()

    results = {"settings": {"p": p, "n_seeds": N_SEEDS, "target_trades": TARGET_TRADES}, **log, "universes": {}}
    reg_rows = []
    for u in ("U1", "U2"):
        r = sw[u]
        dts = list(r["dates"])
        swing_s = pd.Series(r["eq"], index=pd.to_datetime(dts))
        swing_s = swing_s[~swing_s.index.duplicated()]
        ew_full = ew_bh(dts, U[u], SCR[u])
        spy = pd.read_csv(SCR[u] / "SPY.csv", usecols=["date", "close"], index_col=0, parse_dates=True)["close"]
        spy = spy[~spy.index.duplicated()].reindex(pd.to_datetime(dts)).ffill()
        spy_s = CASH * spy / spy.dropna().iloc[0]
        rands = [x for x in rr if x["uni"] == u]
        ud = {}
        for (uk, wn), d0 in W.items():
            if uk != u:
                continue
            ts0 = pd.Timestamp(d0)
            wd = {"SWING": seg(dts, r["eq"], r["expo"], r["trades"], d0),
                  "EW_BH": metrics_series(ew_full, d0), "SPY": metrics_series(spy_s, d0)}
            wd["SWING"]["start"] = str(d0)
            g = lambda k: np.array([x["wins"][wn][k] for x in rands])  # noqa: E731
            sh, cg, tr, dd, ex, wr, tpd = (g("sharpe"), g("cagr"), g("trades"), g("max_dd"), g("exposure"),
                                           g("win_rate"), g("trades_per_day"))
            mi = int(np.argsort(sh)[len(sh) // 2])
            wd["RANDOM"] = {"sharpe_median": pct(sh, 50), "sharpe_p5": pct(sh, 5), "sharpe_p95": pct(sh, 95),
                            "cagr_median": pct(cg, 50), "cagr_p5": pct(cg, 5), "cagr_p95": pct(cg, 95),
                            "trades_median": pct(tr, 50), "trades_per_day_median": pct(tpd, 50),
                            "win_rate_median": pct(wr, 50), "max_dd_median": pct(dd, 50), "exposure_median": pct(ex, 50),
                            "median_seed": rands[mi]["seed"],
                            "swing_sharpe_pctile_rank": float((sh < wd["SWING"]["sharpe"]).mean() * 100),
                            "sharpes": sh.tolist(), "cagrs": cg.tolist()}
            sr = swing_s[swing_s.index >= ts0].pct_change().dropna()
            er = ew_full[ew_full.index >= ts0].pct_change().dropna()
            pr = spy_s[spy_s.index >= ts0].pct_change().dropna()
            for nm, o in (("EW_BH", er), ("SPY", pr)):
                al = pd.concat([sr, o], axis=1, join="inner").dropna()
                ci, pb = boot_ci(al.iloc[:, 1].values, al.iloc[:, 0].values)   # sharpe(SWING)-sharpe(other)
                wd[f"diff_SWING_minus_{nm}"] = {"diff": sharpe(al.iloc[:, 0].values) - sharpe(al.iloc[:, 1].values),
                                                "CI95": ci, "P_gt0": pb, "corr": float(al.corr().iloc[0, 1])}
            wd["rules"] = {"edge_vs_random": bool(wd["SWING"]["sharpe"] > wd["RANDOM"]["sharpe_p95"]),
                           "beats_EW_BH": bool(wd["diff_SWING_minus_EW_BH"]["CI95"][0] > 0)}
            ud[wn] = wd
            if "full" in wn or u == "U2":
                reg_rows.append((u, wn, wd))
                for nm, s in (("SWING", swing_s), ("EW_BH", ew_full), ("SPY", spy_s)):
                    s[s.index >= ts0].round(2).rename("equity").to_csv(OUT / f"equity_{nm}_{u}.csv", index_label="date")
                m = rands[mi]
                z = pd.Series(m["eq"], index=pd.to_datetime(m["dates"]))
                z = z[~z.index.duplicated()]
                z[z.index >= ts0].round(2).rename("equity").to_csv(OUT / f"equity_RANDOM_median_{u}.csv", index_label="date")
        results["universes"][u] = ud
    results["runtime_s"] = round(time.time() - t_start, 1)
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")

    reg = ROOT / "data" / "experiments" / "trials_registry.csv"
    shutil.copy2(reg, reg.with_name(f"trials_registry.backup_{datetime.now():%Y%m%d_%H%M%S}.csv"))
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with open(reg, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for u, wn, wd in reg_rows:
            uname = "live53_ext" if u == "U1" else "live87"
            rn = wd["RANDOM"]
            for strat, mm, note in (
                    ("SWING", wd["SWING"], "variant d $500k 0.5% risk; turnover col = trade count"),
                    ("EW_BH", wd["EW_BH"], "equal-weight monthly rebalance buy&hold; turnover col = 0 (n/a)"),
                    ("SPY", wd["SPY"], "SPY buy&hold; turnover col = 0 (n/a)")):
                w.writerow([now, "swing_baseline", f"{strat}_{u}",
                            hashlib.sha1(f"swing_baseline|{strat}|{u}".encode()).hexdigest()[:10],
                            uname, mm["from"], mm["to"], round(mm["cagr"], 4), round(mm["sharpe"], 3),
                            round(-mm["max_dd"], 4), mm["trades"] if mm["trades"] is not None else 0, note])
            w.writerow([now, "swing_baseline", f"RANDOM_{u}",
                        hashlib.sha1(f"swing_baseline|RANDOM|{u}".encode()).hexdigest()[:10],
                        uname, wd["SWING"]["from"], wd["SWING"]["to"], round(rn["cagr_median"], 4),
                        round(rn["sharpe_median"], 3), round(-rn["max_dd_median"], 4), int(rn["trades_median"]),
                        f"median of {N_SEEDS} seeds; p={p:.4f}; n_seeds={N_SEEDS}; sharpe p5/p95={rn['sharpe_p5']:.3f}/{rn['sharpe_p95']:.3f}; "
                        f"cagr p5/p95={rn['cagr_p5']:.4f}/{rn['cagr_p95']:.4f}; turnover col = trade count"])
    for k in SCR.values():
        shutil.rmtree(k, ignore_errors=True)
    write_report(results)
    print("DONE", results["runtime_s"], flush=True)
    return 0


def f(x, n=2):
    return "n/a" if x is None else f"{x:.{n}f}"


def write_report(R):
    L = ["# Swing baseline study (2026-11-28)", "",
         "Does live swing variant d have an edge, or does it ride the market? Same data, costs, sizing, holdout cut (<= 2025-11-21) as swing_universe_study.", "",
         "## Pre-declared decision rules (verbatim)",
         "- Entry signal has an edge only if SWING's Sharpe is above the 95th percentile of the 50 RANDOM seeds in that window.",
         "- SWING beats buy-and-hold only if the 95% CI of (SWING - EW_BH) Sharpe difference is entirely above 0.",
         "- The full 2011-2025 U1 window is the primary test; last3y and U2 are supporting.", "", "## Verdicts"]
    for u, ud in R["universes"].items():
        for wn, wd in ud.items():
            ru = wd["rules"]
            L.append(f"- {u} {wn}: edge vs RANDOM = **{ru['edge_vs_random']}** (SWING Sharpe {f(wd['SWING']['sharpe'])} vs RANDOM p95 {f(wd['RANDOM']['sharpe_p95'])}, "
                     f"percentile rank {f(wd['RANDOM']['swing_sharpe_pctile_rank'],0)}); beats EW_BH = **{ru['beats_EW_BH']}** "
                     f"(diff {f(wd['diff_SWING_minus_EW_BH']['diff'])}, CI {[round(x,2) for x in wd['diff_SWING_minus_EW_BH']['CI95']]})")
    L += ["", f"Sanity check vs existing report reproduced: {R['sanity']['reproduced']} ({R['sanity']['got']})",
          f"RANDOM entry probability p = {R['p']:.4f} per eligible symbol-day; pilot (seed 0, U1 full): {R['pilot']}", ""]
    for u, ud in R["universes"].items():
        for wn, wd in ud.items():
            L += [f"## {u} - {wn} (start {wd['SWING']['start']})", "",
                  "| strategy | trades | trades/day | win rate | CAGR | Sharpe | maxDD | avg exposure |", "|---|---|---|---|---|---|---|---|"]
            s = wd["SWING"]
            L.append(f"| SWING | {s['trades']} | {f(s['trades_per_day'],3)} | {f(s['win_rate'])} | {f(s['cagr']*100,1)}% | {f(s['sharpe'])} | {f(s['max_dd']*100,1)}% | {f(s['exposure'])} |")
            r = wd["RANDOM"]
            L.append(f"| RANDOM median | {f(r['trades_median'],0)} | {f(r['trades_per_day_median'],3)} | {f(r['win_rate_median'])} | {f(r['cagr_median']*100,1)}% | {f(r['sharpe_median'])} | {f(r['max_dd_median']*100,1)}% | {f(r['exposure_median'])} |")
            for nm in ("EW_BH", "SPY"):
                m = wd[nm]
                L.append(f"| {nm} | - | - | - | {f(m['cagr']*100,1)}% | {f(m['sharpe'])} | {f(m['max_dd']*100,1)}% | ~1.00 |")
            L += ["", f"RANDOM Sharpe p5/median/p95: {f(r['sharpe_p5'])} / {f(r['sharpe_median'])} / {f(r['sharpe_p95'])}; "
                  f"CAGR p5/median/p95: {f(r['cagr_p5']*100,1)}% / {f(r['cagr_median']*100,1)}% / {f(r['cagr_p95']*100,1)}%; SWING Sharpe percentile rank among seeds: {f(r['swing_sharpe_pctile_rank'],0)}.", ""]
            for nm in ("EW_BH", "SPY"):
                d = wd[f"diff_SWING_minus_{nm}"]
                L.append(f"- SWING minus {nm} Sharpe: {f(d['diff'])}, 95% CI [{f(d['CI95'][0])}, {f(d['CI95'][1])}], P(diff>0)={f(d['P_gt0'])}, daily-return corr {f(d['corr'])}")
            L.append("")
    L += ["## Method and caveats",
          "- Engine: run_backtest with live variant d (build_settings({**D, **SIZE})), SEC fee 2.78e-5, TAF 0.000166, benchmark SPY. Bars cut at 2025-11-21 via cut_bars.",
          "- Windows share one full run per universe. Common start = day before SWING's first entry in the window (>= window start); all strategies are measured from that date. The sanity check uses the existing script's start (2011-01-03).",
          "- U1 = live symbols present in bars_yahoo_ext (current live list: survivorship/coverage bias). U2 = live87 universe in bars_yahoo (about 4 years of data only).",
          "- RANDOM: signal_fn wrapper; entry draw with prob p per (seed,symbol,date) via md5 hash (order independent), then real eligibility is kept; stop = ta.levels (2xATR, clamped); tp from ta.levels (dropped by engine when bracket_take_profit is off); ta_score uniform random ranks candidates. The real consensus gate/regime filters are bypassed. p calibrated once on U1 full window (seed 0) and reused for U2 and all seeds, so U2 trade counts are not matched to SWING.",
          "- EW_BH: equal weight over symbols with a bar on the rebalance day, rebalanced at the close of the first trading day of each month, close column (same as engine), sell-side SEC fee + TAF applied; no slippage/spread. SPY: buy and hold close.",
          "- Bootstrap: paired 21-day blocks, 2000 resamples, seed 7 (boot_ci). Percentile rank = share of seeds with Sharpe below SWING."]
    (OUT / "REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
