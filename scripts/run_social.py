#!/usr/bin/env python3
"""Run every social-media strategy trial + benchmarks -> data/experiments/social/results.csv (+ registry append).
Research data only (<= 2025-11-23). Usage: python scripts/run_social.py [--no-registry]"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.rotation_lab import social as S  # noqa: E402

OUT = ROOT / "data" / "experiments" / "social"
SECT = "XLB XLE XLF XLI XLK XLP XLU XLV XLY".split()
LONG_START = pd.Timestamp("2006-01-03")
COMMON_START = pd.Timestamp("2011-01-03")
SVXY_START = pd.Timestamp("2012-10-01")  # one year after SVXY/UVXY listing (2011-10) for warm-up


def build_panel():
    P = S.Panel()
    for s in ["SPY", "QQQ", "TLT", "IEF", "SMH"] + SECT:
        P.add(s)
    P.add_lev("UPRO", "SPY", 3, real="UPRO")
    P.add_lev("TQQQ", "QQQ", 3, real="TQQQ")
    P.add_lev("TMF", "TLT", 3, real="TMF")
    P.add_lev("SSO", "SPY", 2, real="SSO")
    P.add_lev("QLD", "QQQ", 2, real="QLD")
    P.add_lev("SOXL", "SMH", 3, real="SOXL")
    P.add("SVXY")
    return P


def trials(P):
    T = []  # (id, family, label, source, W-or-(W,flag), eval_start, universe, notes)

    def add(fam, label, src, w, start=LONG_START, uni="", notes=""):
        T.append(dict(fam=fam, label=label, src=src, w=w, start=start, uni=uni, notes=notes))

    # S01 Hedgefundie
    for lab, a, rb in [("55/45 q", (0.55, 0.45), "quarterly"), ("40/60 q", (0.40, 0.60), "quarterly"),
                       ("60/40 q", (0.60, 0.40), "quarterly"), ("55/45 monthly", (0.55, 0.45), "monthly"),
                       ("55/45 annual", (0.55, 0.45), "annual")]:
        add("S01_HFEA", lab, "bogleheads t=272007/288192", S.w_static(P, {"UPRO": a[0], "TMF": a[1]}, rb),
            uni="UPRO,TMF synth<2009")
    add("S01_HFEA", "55/45 UPRO/BIL q", "bogleheads (Quanterlab control)", S.w_static(P, {"UPRO": 0.55}, "quarterly"),
        uni="UPRO,BIL")
    # S02 TQQQ 200-SMA
    add("S02_TQQQ_SMA", "QQQ200 +5/-3 ->BIL", "tradingview VfrwgjA6", S.w_trend(P, "QQQ", "TQQQ", "CASH", 200, 0.05, 0.03), uni="TQQQ")
    add("S02_TQQQ_SMA", "QQQ200 plain ->BIL", "tradingview", S.w_trend(P, "QQQ", "TQQQ", "CASH", 200), uni="TQQQ")
    add("S02_TQQQ_SMA", "QQQ150 plain ->BIL", "variant", S.w_trend(P, "QQQ", "TQQQ", "CASH", 150), uni="TQQQ")
    add("S02_TQQQ_SMA", "SPY200 +4/-3 ->BIL", "r/LETFs 1nhye66", S.w_trend(P, "SPY", "TQQQ", "CASH", 200, 0.04, 0.03), uni="TQQQ")
    add("S02_TQQQ_SMA", "SPY200 +4/-3 ->QQQ", "r/LETFs 1nhye66 (DCA-to-QQQ simplified to switch)", S.w_trend(P, "SPY", "TQQQ", "QQQ", 200, 0.04, 0.03), uni="TQQQ,QQQ")
    add("S02_TQQQ_SMA", "TQQQ own 200 ->BIL", "composer 50d/200d family", S.w_trend(P, "TQQQ", "TQQQ", "CASH", 200), uni="TQQQ")
    # S03 other LETF trend + buy-and-hold hype checks
    add("S03_LETF_TREND", "UPRO spy200 plain", "r/LETFs common", S.w_trend(P, "SPY", "UPRO", "CASH", 200), uni="UPRO")
    add("S03_LETF_TREND", "UPRO spy200 +3/-3", "variant", S.w_trend(P, "SPY", "UPRO", "CASH", 200, 0.03, 0.03), uni="UPRO")
    add("S03_LETF_TREND", "SSO spy200 plain", "r/LETFs common", S.w_trend(P, "SPY", "SSO", "CASH", 200), uni="SSO")
    add("S03_LETF_TREND", "QLD qqq200 plain", "r/LETFs common", S.w_trend(P, "QQQ", "QLD", "CASH", 200), uni="QLD")
    add("S03_LETF_TREND", "TQQQ buy&hold", "hype check", S.w_static(P, {"TQQQ": 1.0}, "annual"), uni="TQQQ")
    add("S03_LETF_TREND", "UPRO buy&hold", "hype check", S.w_static(P, {"UPRO": 1.0}, "annual"), uni="UPRO")
    # S04 FTLT approx
    for hot, cold, d in [(80, 30, "CASH"), (79, 31, "CASH"), (82, 30, "CASH"), (80, 30, "TLT"), (75, 30, "CASH"), (85, 25, "TLT")]:
        add("S04_FTLT", f"hot{hot} cold{cold} def{d}", "composer TQQQ-FTLT clones (approx)", S.w_ftlt(P, hot, cold, d), uni="TQQQ")
    # S05 9Sig
    for tg, iw in [(0.09, 0.6), (0.09, 0.8), (0.06, 0.6), (0.12, 0.6), (0.09, 0.4)]:
        add("S05_9SIG", f"{int(tg*100)}% init{iw}", "jasonkelly.com / azqato (approx, no spike/30-down)", S.w_9sig(P, tg, iw), uni="TQQQ,IEF")
    # S06 RSI2
    for a, th, ex, f in [("SPY", 10, "sma5", 200), ("SPY", 5, "sma5", 200), ("SPY", 10, "rsi70", 200), ("SPY", 10, "sma5", None),
                         ("QQQ", 10, "sma5", 200), ("QQQ", 5, "sma5", 200)]:
        add("S06_RSI2", f"{a} <{th} {ex} f{f}", "stockcharts RSI(2); empiricalab", S.w_rsi2(P, a, th, ex, f), uni=a)
    # S07 IBS
    for a, lo, hi, f in [("SPY", .2, .8, None), ("SPY", .1, .8, None), ("SPY", .2, .8, 200), ("QQQ", .2, .8, None), ("QQQ", .1, .8, None), ("QQQ", .2, .8, 200)]:
        add("S07_IBS", f"{a} <{lo} >{hi} f{f}", "Pagonidis 2014; quantifiedstrategies; empiricalab", S.w_ibs(P, a, lo, hi, f), uni=a)
    # S08 down days
    for a, k, h, f in [("SPY", 3, 1, None), ("SPY", 3, 1, 200), ("SPY", 4, 1, None), ("QQQ", 3, 1, None), ("SPY", 2, 1, None), ("SPY", 3, 3, None)]:
        add("S08_DOWNDAYS", f"{a} {k}down hold{h} f{f}", "quantifiedstrategies '3 days down'", S.w_down_days(P, a, k, h, f), uni=a)
    # S09 turn of month
    for a, l, fi, f in [("SPY", 3, 3, None), ("SPY", 5, 3, None), ("SPY", 2, 3, None), ("SPY", 3, 3, 200), ("QQQ", 3, 3, None), ("SPY", 1, 4, None)]:
        add("S09_TOM", f"{a} last{l}+first{fi} f{f}", "quantifiedstrategies; algoedge; tradingview honestcowboy", S.w_tom(P, a, l, fi, f), uni=a)
    # S10 VIX ratio
    for a, th, off, kind, st, lab in [("SPY", 1.0, "CASH", "vix3m", LONG_START, "SPY while VIX/VIX3M<1.0"),
                                      ("SPY", 0.95, "CASH", "vix3m", LONG_START, "SPY while VIX/VIX3M<0.95"),
                                      ("SPY", 0.90, "CASH", "vix3m", LONG_START, "SPY while VIX/VIX3M<0.90"),
                                      ("SVXY", 0.95, "CASH", "vix3m", SVXY_START, "SVXY while VIX/VIX3M<0.95"),
                                      ("SVXY", 1.0, "CASH", "vix3m", SVXY_START, "SVXY while VIX/VIX3M<1.0"),
                                      ("SVXY", 1.0, "CASH", "vix9d", SVXY_START, "SVXY while VIX9D<VIX")]:
        add("S10_VIX_TERM", lab, "QC forum 2657/7984; Concretum; tradingview VIX9D", S.w_vix_ratio(P, a, th, off, kind), start=st,
            uni=a, notes="SVXY pre-2018-02 is -1x, after -0.5x (Yahoo series)" if a == "SVXY" else "")
    # S11 VIX leverage scaling
    for lo, hi, lm in [(15, 30, 1.5), (12, 25, 1.5), (20, 35, 1.5), (15, 30, 2.0), (15, 30, 1.25)]:
        add("S11_VIX_LEV", f"lo{lo} hi{hi} Lmax{lm}", "QC research 20886", S.w_vix_lev(P, lo, hi, lm), uni="SPY,SSO")
    # S12 LETF momentum
    for names, lk, rb in [(["TQQQ", "UPRO", "TMF"], 126, "monthly"), (["TQQQ", "UPRO", "TMF"], 63, "monthly"),
                          (["TQQQ", "UPRO", "TMF"], 252, "monthly"), (["TQQQ", "UPRO", "SOXL", "TMF"], 126, "monthly"),
                          (["TQQQ", "UPRO", "TMF"], 126, "quarterly")]:
        add("S12_LETF_MOM", f"{'/'.join(names)} look{lk} {rb}", "composer/r-LETFs pattern (no single post)", S.w_letf_mom(P, names, lk, rb), uni=",".join(names))
    # S13 sector rotation
    for n, lk, sk, f in [(3, 63, 0, True), (3, 252, 21, False), (3, 252, 21, True), (4, 126, 0, True), (3, 126, 0, False), (2, 252, 21, True)]:
        add("S13_SECTOR", f"top{n} look{lk} skip{sk} filt{f}", "tradewink; paperswithbacktest; github sector-momentum", S.w_sector_top(P, SECT, n, lk, sk, f),
            start=COMMON_START, uni="9 SPDR sectors")
    return T


def stage2():
    """Rotation-lab benchmark (12-1 momentum top-10) + stock-basket social rules. Returns (bench dict, trial dicts)."""
    from app.rotation_lab.engine import Spec
    from app.rotation_lab.sweep import Data, run_spec
    from app.strategy_lab.social_families import run_basket
    d = Data()
    bench, ex = {}, []
    for reb in ("monthly", "weekly"):
        r, t = run_spec(d, Spec(universe="sp500", score="mom121", topn=10, rebal=reb, riskoff="BIL"))
        bench[f"MOM121_top10_{reb}_sp500"] = (r, t, COMMON_START)
    for kind, ths in (("rsi2", (10, 5)), ("ibs", (0.2, 0.1))):
        for slots in (10, 20):
            for th in ths:
                r, t = run_basket(d, "sp500", kind, th, slots, 10)
                ex.append(dict(fam=f"S14_BASKET_{kind.upper()}", label=f"sp500 th{th} slots{slots}", src="Connors/Alvarez RSI2; Pagonidis IBS (basket form)",
                               ret=(r, t), start=COMMON_START, uni="sp500_today", notes="survivorship-biased univ; basket form"))
    return bench, ex


def main():
    P = build_panel()
    spy_r = P.close("SPY").pct_change().fillna(0.0)
    tr = trials(P)
    bench2, ex2 = stage2()
    tr += ex2
    n_reg = S.registry_rows()
    n_total = n_reg + len(tr) + S.PRIOR_ROTATION_TRIALS
    print("registry rows", n_reg, "mine", len(tr), "N_total", n_total)
    rows, reg, rets = [], [], {}
    bench = {"SPY_buyhold": S.w_static(P, {"SPY": 1.0}, "annual"),
             "60_40_SPY_IEF": S.w_static(P, {"SPY": .6, "IEF": .4}, "monthly"),
             "SPY_200SMA_1x": (S.w_trend(P, "SPY", "SPY", "CASH", 200), None)}
    for k, w in bench.items():
        W, fl = w if isinstance(w, tuple) else (w, None)
        r, t = S.simulate(P, W, fl)
        rets[k] = (r, t, LONG_START)
    rets.update(bench2)
    for i, x in enumerate(tr):
        if "ret" in x:
            r, t = x["ret"]
        else:
            W, fl = x["w"] if isinstance(x["w"], tuple) else (x["w"], None)
            r, t = S.simulate(P, W, fl)
        rets[f"{x['fam']}|{x['label']}"] = (r, t, x["start"])
        x["i"] = i
    for key, (r, t, st) in rets.items():
        for wname, wstart in (("long", st), ("common", max(st, COMMON_START)), ("app87win", pd.Timestamp("2022-07-01"))):
            m = S.metrics(r, spy_r, t, start=wstart)
            rr = r[r.index >= wstart]
            m["dsr"] = S.deflated_sharpe(rr, n_total, S.SR_VAR) if not key.startswith(("SPY_", "60_", "MOM121")) else float("nan")
            m.update(name=key, window=wname)
            rows.append(m)
    df = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "results.csv", index=False)
    # yearly returns for the record
    yr = pd.DataFrame({k: (1 + v[0][v[0].index >= LONG_START]).groupby(v[0][v[0].index >= LONG_START].index.year).prod() - 1 for k, v in rets.items()})
    yr.to_csv(OUT / "yearly_returns.csv")
    if "--no-registry" not in sys.argv:
        ts = S.now_ts()
        for x in tr:
            key = f"{x['fam']}|{x['label']}"
            m = df[(df.name == key) & (df.window == "long")].iloc[0]
            reg.append(dict(ts=ts, lab="social", strategy=key, config_hash=S.config_hash(key, x["src"]), universe=x["uni"],
                            start=m.start, end=m.end, cagr=round(m.cagr, 4), sharpe=round(m.sharpe, 3), maxdd=round(m.maxdd, 4),
                            turnover=round(m.turnover_yr, 2), notes=("synthetic leveraged pre-inception; " + x["notes"]).strip("; ")))
        S.append_registry(reg)
        print("appended", len(reg))
    pd.set_option("display.width", 250, "display.max_rows", 500, "display.max_colwidth", 60)
    c = ["name", "cagr", "sharpe", "maxdd", "worst_year", "dsr", "blocks"]
    for w in ("long", "common", "app87win"):
        print("==", w)
        print(df[df.window == w].sort_values("sharpe", ascending=False)[c].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
