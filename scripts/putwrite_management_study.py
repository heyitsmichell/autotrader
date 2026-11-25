"""Put-write MANAGEMENT variants (tastytrade 45DTE/50%/21DTE) vs live rule. Black-Scholes model.
Reuses scripts/options_research.py (read-only). Data hard-clipped at 2025-11-21 (locked holdout).
Run: .venv/Scripts/python.exe scripts/putwrite_management_study.py
"""
from __future__ import annotations
import csv, gzip, hashlib, json, math, shutil, sys
from datetime import datetime
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
import options_research as orr  # noqa: E402

OUT = ROOT / "data" / "experiments" / "putwrite_mgmt"
REG = orr.REG
SPREAD = 0.03
# name: (dte, delta, tp, roll_dte, stop)
VARIANTS = {
    "A": (30, .30, None, None, None), "B": (45, .30, None, None, None),
    "C": (45, .30, .5, None, None), "D": (45, .30, None, 21, None),
    "E": (45, .30, .5, 21, None), "F": (30, .30, .5, None, None),
    "G": (45, .30, .5, 21, 2.0),
    "A20": (30, .20, None, None, None), "E20": (45, .20, .5, 21, None),
}


def run(mk, sym, dte, delta, tp, roll, stop):
    S = mk.close[sym]; D = mk.dates; n = len(D)
    eq = [np.nan] * n; eq[0] = orr.ACCOUNT
    c = orr.ACCOUNT
    pos = None
    opens = 0; cost_tot = 0.0; reasons = {}
    reopen_at = 0

    def open_pos(o):
        nonlocal opens, cost_tot
        iv = mk.iv(sym, o)
        x, _ = orr.expiry_idx(mk, o, dte)
        exp = D[x]
        T = max((exp - D[o]).days, 1) / 365.0
        K = orr.strike_for_put_delta(S[o], T, iv, delta, mk.rf[o])
        prem = orr.bs_put(S[o], K, T, iv, mk.rf[o])
        qty = c / K
        n_ct = qty * (10 if sym == "SPY" else 1) / 100.0
        cost = 0.5 * SPREAD * prem * qty + orr.COMM * n_ct
        opens += 1; cost_tot += cost
        return dict(K=K, exp=exp, x=x, qty=qty, prem=prem, prem_cash=prem * qty - cost, o=o, n_ct=n_ct)

    for t in range(n):
        if t > 0:
            c *= math.exp(mk.rf[t - 1] * (D[t] - D[t - 1]).days / 365.0)
        last = t == n - 1
        if pos is not None and t > pos["o"]:
            K, q = pos["K"], pos["qty"]
            if t >= pos["x"] or last:
                mark = max(K - S[t], 0.0)
                c += pos["prem_cash"] - mark * q
                eq[t] = c; pos = None; reopen_at = t
                reasons["expiry"] = reasons.get("expiry", 0) + 1
            else:
                Tt = max((pos["exp"] - D[t]).days, 0) / 365.0
                mark = orr.bs_put(S[t], K, Tt, mk.iv(sym, t), mk.rf[t])
                why = None
                if stop and mark >= stop * pos["prem"]:
                    why, nxt = "stop", t + 1
                elif tp and mark <= tp * pos["prem"]:
                    why, nxt = "tp", t + 1
                elif roll and (pos["exp"] - D[t]).days <= roll:
                    why, nxt = "roll", t
                if why:
                    cc = 0.5 * SPREAD * mark * q + orr.COMM * pos["n_ct"]
                    cost_tot += cc
                    c += pos["prem_cash"] - mark * q - cc
                    eq[t] = c; pos = None; reopen_at = nxt
                    reasons[why] = reasons.get(why, 0) + 1
                else:
                    eq[t] = c + pos["prem_cash"] - mark * q
        elif pos is None:
            eq[t] = c if t > 0 else orr.ACCOUNT
        if pos is None and t >= reopen_at and not last:
            pos = open_pos(t)
            if np.isnan(eq[t]):
                eq[t] = c
        if np.isnan(eq[t]):
            eq[t] = c
    return eq, dict(trades=opens, cost=cost_tot, reasons=reasons)


def extra(mk, eq):
    e = np.array(eq); r = np.diff(e) / e[:-1]; d = mk.dates[1:]
    vol = float(r.std(ddof=1) * math.sqrt(252))
    dd = math.sqrt((np.minimum(r, 0) ** 2).mean())
    sortino = float(r.mean() / dd * math.sqrt(252)) if dd > 0 else 0.0
    mon = {}
    for x, y in zip(d, r):
        mon.setdefault((x.year, x.month), []).append(1 + y)
    pm = float(np.mean([np.prod(v) > 1 for v in mon.values()]))
    return vol, sortino, pm, r


def sh(r):
    return r.mean() / r.std(ddof=1) * math.sqrt(252)


def boot(ra, rb, rng, B=2000, blk=21):
    n = len(ra); nb = math.ceil(n / blk); diffs = np.empty(B)
    for i in range(B):
        st = rng.integers(0, n - blk + 1, nb)
        idx = (st[:, None] + np.arange(blk)).ravel()[:n]
        diffs[i] = sh(rb[idx]) - sh(ra[idx])
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def pct(x):
    return f"{x*100:.1f}%"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    mk = orr.Mkt(); assert max(mk.dates) < orr.HOLDOUT
    rng = np.random.default_rng(7)
    res, rets = {}, {}
    for sym in ("QQQ", "SPY"):
        for v, (dte, dl, tp, roll, stop) in VARIANTS.items():
            eq, info = run(mk, sym, dte, dl, tp, roll, stop)
            m, _, _ = orr.metrics(mk, eq, f"{v}_{sym}")
            vol, so, pm, r = extra(mk, eq)
            m.update(vol=vol, sortino=so, pos_months=pm, **info)
            res[(v, sym)] = m; rets[(v, sym)] = r
            with gzip.open(OUT / f"daily_{v}_{sym}.csv.gz", "wt", newline="") as f:
                w = csv.writer(f); w.writerow(["date", "ret"])
                for x, y in zip(mk.dates[1:], r):
                    w.writerow([str(x), f"{y:.6f}"])
    ci = {}
    for (v, sym), r in rets.items():
        if v in ("A", "A20"):
            continue
        base = "A20" if v.endswith("20") else "A"
        ci[(v, sym)] = (sh(r) - sh(rets[(base, sym)]), *boot(rets[(base, sym)], r, rng))
    chk = {}; put_idx = None
    try:
        old = json.loads((orr.OUT / "summary.json").read_text())["results"]
        for sym in ("QQQ", "SPY"):
            o = old[f"a_putwrite30_{sym}"]
            chk[sym] = dict(old_cagr=round(o["cagr"], 4), old_sharpe=round(o["sharpe"], 3),
                            new_cagr=round(res[("A", sym)]["cagr"], 4), new_sharpe=round(res[("A", sym)]["sharpe"], 3))
        put_idx = old.get("PUT_index_CBOE(yf)")
    except Exception as ex:
        chk["err"] = str(ex)
    L = ["# Put-write management study (Black-Scholes model)", "", "VERDICT_HERE", ""]
    for sym in ("QQQ", "SPY"):
        L += [f"## {sym}, 2011-01-03 to 2025-11-21 (delta 0.30 unless A20/E20 = 0.20)", "",
              "| var | rule | CAGR | vol | Sharpe | Sortino | maxDD | worst month | %mo>0 | trades | cost $ | 2015-08 | 2018-02 | 2020-03 | 2022 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for v, (dte, dl, tp, roll, stop) in VARIANTS.items():
            m = res[(v, sym)]; e = m["episodes_maxdd"]
            rule = f"{dte}DTE d{dl}" + (f" tp{int(tp*100)}" if tp else "") + (f" roll{roll}" if roll else "") + (f" stop{int(stop*100)}" if stop else "")
            L.append(f"| {v} | {rule} | {pct(m['cagr'])} | {pct(m['vol'])} | {m['sharpe']:.2f} | {m['sortino']:.2f} | {pct(m['maxdd'])} | "
                     f"{pct(m['worst_month'])} ({m['worst_month_label']}) | {pct(m['pos_months'])} | {m['trades']} | {m['cost']:,.0f} | "
                     + " | ".join(pct(e[k]) if k in e else "-" for k in orr.EPISODES) + " |")
        L += ["", f"Sharpe difference vs A (A20 for the 0.20 variants), paired 21-day block bootstrap, 2000 resamples, 95% CI ({sym}):", "",
              "| var | dSharpe | CI low | CI high | excludes 0? |", "|---|---|---|---|---|"]
        for (v, s), (d, lo, hi) in ci.items():
            if s == sym:
                L.append(f"| {v} | {d:+.2f} | {lo:+.2f} | {hi:+.2f} | {'yes' if lo > 0 or hi < 0 else 'no'} |")
        L.append("")
    L += ["## Exit reasons (count)", ""] + [f"- {v}_{s}: {res[(v, s)]['reasons']}" for (v, s) in res]
    L += ["", "## Reproduction check: A vs existing a_putwrite30", "", f"`{chk}`"]
    if put_idx:
        L += ["", f"Real CBOE PUT index (yfinance): CAGR {pct(put_idx['cagr'])}, Sharpe {put_idx['sharpe']:.2f}, maxDD {pct(put_idx['maxdd'])}."]
    L += ["", "## Caveats", "",
          "- Black-Scholes, no skew. SPY IV = VIX. The existing model's QQQ IV is VIX scaled by the 60d RV ratio QQQ/SPY (clipped 0.6-2.0), not real QQQ IV; real QQQ IV is usually higher than VIX, so QQQ premiums here are likely understated.",
          "- The earlier modelled SPY put-write (Sharpe 0.78) overstated the real CBOE PUT index (Sharpe 0.53). Absolute Sharpe is optimistic; only the relative comparison between rules is informative, and it shares the same model errors.",
          "- Costs: spread_rt 3% of premium (half per side) + $0.65/contract/side on each open and each early close; expiry settlement is free. Take-profit/stop reopen the next trading day, roll reopens the same day. Early-close fills assumed at the daily close mark.",
          "- Take-profit and stop are tested on daily closes only. Cycles use calendar-day DTE, not exchange expiry dates.",
          "- Multiple testing: 18 trials added to the registry; the variants are highly correlated, so do not pick the best off a single Sharpe.",
          "- Holdout after 2025-11-21 never loaded."]
    (OUT / "REPORT.md").write_text("\n".join(L), encoding="utf-8")
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy(REG, REG.with_name(f"trials_registry.backup_{ts}.csv"))
    rows = []; t0 = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for (v, sym), m in res.items():
        k = f"putwrite_mgmt_{v}_{sym}"
        rows.append([t0, "putwrite_mgmt", k, hashlib.sha1(k.encode()).hexdigest()[:10], sym, m["start"], m["end"],
                     round(m["cagr"], 4), round(m["sharpe"], 3), round(m["maxdd"], 4), m["trades"],
                     "BS-modelled, VIX-based IV, cash-secured; management variant study"])
    with open(REG, "a", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)
    print("\n".join(L[4:]))
    print("registered", len(rows))


if __name__ == "__main__":
    main()
