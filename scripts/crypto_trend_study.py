"""Crypto trend via spot ETFs (IBIT/ETHA) + BTC option overlays. Research only (2026-11-27).

Question: can the Botsfolio "BTC & ETH Cycle Trader" idea (spot, no leverage, in when trending, cash when not)
be run through the US spot ETFs, and do options on top help?

Rules are PRE-DECLARED below (66 trend trials + option trials), every trial goes to the trials registry.
Data hard-clipped at 2025-11-21 (locked holdout, HANDOFF §3). Proxy: Yahoo BTC-USD / ETH-USD daily closes
sampled on NYSE trading days (an ETF can only trade then). Signal uses closes up to t-1 and earns the return
from t to t+1 (2-bar lag, conservative: Yahoo crypto closes are 00:00 UTC, after the 16:00 ET ETF close).
Costs: 0.05% per side on every weight change, 0.25%/yr ETF fee on the held weight, cash earns 13-week T-bill.
Options: Black-Scholes; IV = Deribit DVOL (2021-03 on), before that 30d RV x the median DVOL/RV ratio of the
overlap; 5% of premium round trip + $0.65/contract/side; contract = 100 IBIT shares (1 share ~ BTC/1760).

Run: .venv/Scripts/python.exe scripts/crypto_trend_study.py   (needs internet once; caches raw data)
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "experiments" / "crypto_trend"
RAW = OUT / "raw"
REG = ROOT / "data" / "experiments" / "trials_registry.csv"
END = date(2025, 11, 21)            # locked holdout starts after this
HOLDOUT = date(2025, 11, 23)
ND = NormalDist()

SIDE_COST = 0.0005
ETF_FEE = 0.0025
OPT_SPREAD_RT = 0.05
COMM = 0.65
IBIT_PER_BTC = 1760.0              # ~IBIT shares per BTC (IBIT ~= BTC / 1760)

SMA_N = (50, 100, 150, 200)
TSM_L = (21, 63, 126, 252)
VOL_T = (None, 0.40, 0.25)
UNIVERSES = {"BTC": ["BTC-USD"], "BTC+ETH": ["BTC-USD", "ETH-USD"]}
EVAL_START = {"BTC": "2016-01-04", "BTC+ETH": "2018-07-02"}   # after 252 bars of history
ROBUST = "ens_all|vt40"            # pre-declared default (not picked on results): 8-signal ensemble, 40% vol target


# ------------------------------------------------------------------ data
def _yahoo(sym: str) -> pd.Series:
    f = RAW / f"{sym.replace('^', '')}.csv"
    if not f.exists():
        import yfinance as yf
        RAW.mkdir(parents=True, exist_ok=True)
        df = yf.download(sym, start="2014-01-01", end=HOLDOUT.isoformat(), auto_adjust=True, progress=False)
        s = df["Close"].squeeze().dropna()
        s.index = pd.to_datetime(s.index).tz_localize(None)
        s.rename("close").to_csv(f, index_label="date")
    s = pd.read_csv(f, index_col="date", parse_dates=True)["close"]
    return s[s.index.date <= END]


def _dvol() -> pd.Series:
    f = RAW / "DVOL_BTC.csv"
    if not f.exists():
        rows = []
        for a, b in (("2021-01-01", "2023-06-30"), ("2023-07-01", END.isoformat())):
            t0 = int(datetime.fromisoformat(a).timestamp() * 1000)
            t1 = int(datetime.fromisoformat(b).timestamp() * 1000) + 86_400_000
            u = ("https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC"
                 f"&start_timestamp={t0}&end_timestamp={t1}&resolution=1D")
            req = urllib.request.Request(u, headers={"User-Agent": "research-script"})
            rows += json.load(urllib.request.urlopen(req, timeout=30))["result"]["data"]
        s = pd.Series({pd.Timestamp(r[0], unit="ms").normalize(): r[4] / 100 for r in rows}).sort_index()
        s = s[~s.index.duplicated()]
        s.rename("dvol").to_csv(f, index_label="date")
    s = pd.read_csv(f, index_col="date", parse_dates=True)["dvol"]
    return s[s.index.date <= END]


def load():
    spy = _yahoo("SPY")
    cal = spy.index[(spy.index >= "2014-09-17")]
    px = pd.DataFrame({s: _yahoo(s) for s in ("BTC-USD", "ETH-USD")}).reindex(cal).ffill()
    rf = (_yahoo("^IRX").reindex(cal).ffill().bfill() / 100)
    return px, rf, _dvol().reindex(cal).ffill()


# ------------------------------------------------------------------ trend rules (weights use data <= t)
def signal(p: pd.Series, rule: str) -> pd.Series:
    sma = {n: (p > p.rolling(n).mean()).astype(float).where(p.rolling(n).mean().notna()) for n in SMA_N}
    tsm = {n: (p > p.shift(n)).astype(float).where(p.shift(n).notna()) for n in TSM_L}
    if rule.startswith("sma"):
        return sma[int(rule[3:])]
    if rule.startswith("tsm"):
        return tsm[int(rule[3:])]
    if rule == "ens_sma":
        return pd.concat(sma.values(), axis=1).mean(axis=1, skipna=False)
    if rule == "ens_tsm":
        return pd.concat(tsm.values(), axis=1).mean(axis=1, skipna=False)
    if rule == "ens_all":
        return pd.concat([*sma.values(), *tsm.values()], axis=1).mean(axis=1, skipna=False)
    raise ValueError(rule)


RULES = [f"sma{n}" for n in SMA_N] + [f"tsm{n}" for n in TSM_L] + ["ens_sma", "ens_tsm", "ens_all"]


def _band(w: pd.DataFrame, band: float) -> pd.DataFrame:
    """Only move a weight when the target is > band away from the current one (or goes to 0)."""
    a = w.to_numpy().copy()
    for t in range(1, len(a)):
        keep = (np.abs(a[t] - a[t - 1]) < band) & (a[t] > 0)
        a[t] = np.where(keep, a[t - 1], a[t])
    return pd.DataFrame(a, w.index, w.columns)


def trend_returns(px, rf, syms, rule, vt, lag=2, band=0.0):
    r = px[syms].pct_change().fillna(0)
    w = pd.DataFrame({s: signal(px[s], rule) for s in syms}).fillna(0) / len(syms)
    if vt:
        vol = r.rolling(21).std() * math.sqrt(252)
        w = w * (vt / vol).clip(upper=1.0).fillna(0)
    w = w.shift(lag - 1).fillna(0)                  # w[t] decided on data <= t-(lag-1)
    if band:
        w = _band(w, band)
    held = w.shift(1).fillna(0)                     # earns r[t] with the weight set the bar before
    turn = w.diff().abs().sum(axis=1).fillna(0)
    rc = rf / 252
    ret = (held * r).sum(axis=1) + (1 - held.sum(axis=1)) * rc - turn * SIDE_COST - held.sum(axis=1) * ETF_FEE / 252
    return ret, held.sum(axis=1), turn


def hold_returns(px, syms):
    r = px[syms].pct_change().fillna(0)             # monthly-rebalanced-ish: daily equal weight is fine here
    return r.mean(axis=1) - ETF_FEE / 252


# ------------------------------------------------------------------ metrics
def metrics(ret: pd.Series) -> dict:
    ret = ret.dropna()
    eq = (1 + ret).cumprod()
    yrs = (ret.index[-1] - ret.index[0]).days / 365.25
    sd = ret.std(ddof=1)
    mon = (1 + ret).groupby([ret.index.year, ret.index.month]).prod() - 1
    py = (1 + ret).groupby(ret.index.year).prod() - 1
    return dict(start=str(ret.index[0].date()), end=str(ret.index[-1].date()),
                cagr=float(eq.iloc[-1] ** (1 / yrs) - 1), vol=float(sd * math.sqrt(252)),
                sharpe=float(ret.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0,
                maxdd=float((eq / eq.cummax() - 1).min()), worst_month=float(mon.min()),
                pos_months=float((mon > 0).mean()), per_year={int(k): float(v) for k, v in py.items()},
                skew=float(ret.skew()), kurt=float(ret.kurt() + 3), n=len(ret))


def sharpe(r):
    return r.mean() / r.std(ddof=1) * math.sqrt(252)


def boot_diff(ra: np.ndarray, rb: np.ndarray, rng, B=2000, blk=21):
    n = len(ra)
    nb = math.ceil(n / blk)
    out = np.empty(B)
    for i in range(B):
        st = rng.integers(0, n - blk + 1, nb)
        idx = (st[:, None] + np.arange(blk)).ravel()[:n]
        out[i] = sharpe(rb[idx]) - sharpe(ra[idx])
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def deflated_sharpe(m: dict, trial_srs: list[float]) -> float:
    """Bailey & Lopez de Prado DSR, per-period Sharpe; trial_srs are annualised Sharpes of all trials."""
    n = len(trial_srs)
    srs = np.array(trial_srs) / math.sqrt(252)
    v = float(srs.var(ddof=1)) if n > 1 else 0.0
    g = 0.5772156649
    sr0 = math.sqrt(v) * ((1 - g) * ND.inv_cdf(1 - 1 / n) + g * ND.inv_cdf(1 - 1 / (n * math.e))) if n > 1 else 0.0
    sr = m["sharpe"] / math.sqrt(252)
    den = math.sqrt(max(1e-12, 1 - m["skew"] * sr + (m["kurt"] - 1) / 4 * sr * sr))
    return float(ND.cdf((sr - sr0) * math.sqrt(m["n"] - 1) / den))


# ------------------------------------------------------------------ options (BTC only; IBIT-sized contracts)
def _bs(S, K, T, v, r, call):
    if T <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    c = S * ND.cdf(d1) - K * math.exp(-r * T) * ND.cdf(d2)
    return c if call else c - S + K * math.exp(-r * T)


def _strike(S, T, v, r, delta, call):
    """Strike with |delta| = delta (put: call delta 1-delta)."""
    cd = delta if call else 1 - delta
    d1 = ND.inv_cdf(cd)
    return S * math.exp((r + v * v / 2) * T - d1 * v * math.sqrt(T))


def option_sim(px: pd.Series, iv: pd.Series, rf: pd.Series, gate: pd.Series | None, kind: str,
               delta=0.30, dte=30, close_on_gate_off=False, start="2016-01-04") -> tuple[pd.Series, dict]:
    """kind: 'put' = cash-secured short put; 'cc' = long shares + short call; 'call' = long call delta-notional.
    gate: 0..1 exposure signal decided on data <= t-1 (None = always). Opens only when gate >= 0.5.
    Each cycle sized to current equity. Hold to expiry unless close_on_gate_off."""
    idx = px.loc[start:].index
    S = px.loc[idx].to_numpy()
    V = iv.loc[idx].to_numpy()
    R = rf.loc[idx].to_numpy()
    G = np.ones(len(idx)) if gate is None else gate.shift(1).reindex(idx).fillna(0).to_numpy()
    eq = np.empty(len(idx))
    cash = 1.0
    pos = None
    n_open = 0
    cost_tot = 0.0
    call = kind in ("cc", "call")
    for t in range(len(idx)):
        if t:
            cash *= math.exp(R[t - 1] * (idx[t] - idx[t - 1]).days / 365)
        val = cash
        if pos is not None:
            T = max((pos["exp"] - idx[t]).days, 0) / 365
            mark = _bs(S[t], pos["K"], T, V[t], R[t], call)
            expired = idx[t] >= pos["exp"]
            gate_off = close_on_gate_off and G[t] < 0.5
            if expired or gate_off or t == len(idx) - 1:
                c = 0.0 if expired else 0.5 * OPT_SPREAD_RT * mark * pos["q"] + COMM * pos["q"] * IBIT_PER_BTC / 100
                cost_tot += c
                if kind == "put":
                    cash -= mark * pos["q"] + c
                elif kind == "cc":
                    cash += pos["q"] * S[t] * (1 - SIDE_COST) - mark * pos["q"] - c
                else:
                    cash += mark * pos["q"] - c
                pos = None
                val = cash
            else:
                if kind == "put":
                    val = cash - mark * pos["q"]
                elif kind == "cc":
                    val = cash + pos["q"] * S[t] - mark * pos["q"]
                else:
                    val = cash + mark * pos["q"]
        if pos is None and G[t] >= 0.5 and t < len(idx) - 1:
            T = dte / 365
            exp = idx[t] + pd.Timedelta(days=dte)
            K = _strike(S[t], T, V[t], R[t], delta, call)
            prem = _bs(S[t], K, T, V[t], R[t], call)
            if kind == "put":
                q = cash / K
            elif kind == "cc":
                q = cash / (S[t] * (1 + SIDE_COST))
            else:   # long call, delta-notional = equity (stock replacement)
                q = cash / (0.70 * S[t]) if delta >= 0.5 else cash / S[t]
            n_ct = q * IBIT_PER_BTC / 100
            c = 0.5 * OPT_SPREAD_RT * prem * q + COMM * n_ct
            cost_tot += c
            n_open += 1
            if kind == "put":
                cash += prem * q - c
                val = cash - prem * q
            elif kind == "cc":
                cash -= q * S[t] * (1 + SIDE_COST)
                cash += prem * q - c
                val = cash + q * S[t] - prem * q
            else:
                cash -= prem * q + c
                val = cash + prem * q
            pos = dict(K=K, exp=exp, q=q)
        eq[t] = val
    e = pd.Series(eq, idx)
    return e.pct_change().fillna(0), dict(cycles=n_open, cost=cost_tot)


# ------------------------------------------------------------------ main
def pct(x):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.1f}%"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    px, rf, dvol = load()
    assert px.index.max().date() <= END
    rng = np.random.default_rng(11)
    res, rets, trials = {}, {}, []

    # ---- 1. trend grid
    for u, syms in UNIVERSES.items():
        st = EVAL_START[u]
        rets[(u, "hold")] = hold_returns(px, syms).loc[st:]
        res[(u, "hold")] = metrics(rets[(u, "hold")])
        cash = (rf / 252).loc[st:]
        rets[(u, "cash")] = cash
        for rule in RULES:
            for vt in VOL_T:
                k = f"{rule}|{'vt' + str(int(vt * 100)) if vt else 'raw'}"
                r, expo, turn = trend_returns(px, rf, syms, rule, vt)
                r = r.loc[st:]
                m = metrics(r)
                m.update(avg_expo=float(expo.loc[st:].mean()), turnover=float(turn.loc[st:].sum() / m["n"] * 252))
                res[(u, k)] = m
                rets[(u, k)] = r
                trials.append((u, k))
        # lag-1 sensitivity for the pre-declared default
        r1, _, _ = trend_returns(px, rf, syms, "ens_all", 0.40, lag=1)
        res[(u, ROBUST + "|lag1")] = metrics(r1.loc[st:])
        rb, _, tb = trend_returns(px, rf, syms, "ens_all", 0.40, band=0.10)
        res[(u, ROBUST + "|band10")] = metrics(rb.loc[st:])
        res[(u, ROBUST + "|band10")]["turnover"] = float(tb.loc[st:].sum() / len(rb.loc[st:]) * 252)

    # ---- 2. walk-forward selection (yearly, expanding, pick best Sharpe among the 33 rules)
    wf = {}
    for u in UNIVERSES:
        keys = [k for (uu, k) in trials if uu == u]
        years = sorted({d.year for d in rets[(u, keys[0])].index})
        pieces, picks = [], {}
        for y in years[2:]:
            tr = {k: sharpe(rets[(u, k)][rets[(u, k)].index.year < y]) for k in keys}
            best = max(tr, key=tr.get)
            picks[y] = best
            pieces.append(rets[(u, best)][rets[(u, best)].index.year == y])
        r = pd.concat(pieces)
        wf[u] = (metrics(r), picks, r, metrics(rets[(u, ROBUST)].loc[r.index[0]:]),
                 metrics(rets[(u, "hold")].loc[r.index[0]:]))

    # ---- 3. DSR + bootstrap vs hold for the default and the in-sample best
    srs = {u: [res[(u, k)]["sharpe"] for (uu, k) in trials if uu == u] for u in UNIVERSES}
    stat = {}
    for u in UNIVERSES:
        best = max((k for (uu, k) in trials if uu == u), key=lambda k: res[(u, k)]["sharpe"])
        for k in dict.fromkeys([ROBUST, best]):
            a, b = rets[(u, "hold")].to_numpy(), rets[(u, k)].to_numpy()
            lo, hi = boot_diff(a, b, rng)
            stat[(u, k)] = dict(dsr=deflated_sharpe(res[(u, k)], srs[u] * 2),   # x2: both universes are trials
                                d_sharpe=res[(u, k)]["sharpe"] - res[(u, "hold")]["sharpe"], lo=lo, hi=hi,
                                best=k == best)

    # ---- 4. option overlays on BTC (IBIT): IV from DVOL, calibrated RV before 2021-03
    btc = px["BTC-USD"]
    rv = btc.pct_change().rolling(21).std() * math.sqrt(252)
    ratio = float((dvol / rv).dropna().median())
    iv_base = dvol.where(dvol.notna(), rv * ratio).bfill()
    gate = signal(btc, "ens_all")
    _, expo_def, _ = trend_returns(px, rf, ["BTC-USD"], "ens_all", 0.40)
    opt_rows = {}
    shares_r = rets[("BTC", ROBUST)]
    for ivm in (1.0, 0.85, 1.15):
        iv = iv_base * ivm
        variants = {
            "put_always": dict(gate=None, kind="put"),
            "put_trend": dict(gate=gate, kind="put"),
            "put_trend_close": dict(gate=gate, kind="put", close_on_gate_off=True),
            "put_trend_d20": dict(gate=gate, kind="put", delta=0.20),
            "cc_trend": dict(gate=gate, kind="cc"),
            "call70_trend_close": dict(gate=gate, kind="call", delta=0.70, close_on_gate_off=True),
        }
        for name, kw in variants.items():
            r, info = option_sim(btc, iv, rf, start=EVAL_START["BTC"], **kw)
            m = metrics(r)
            m.update(info)
            opt_rows[(name, ivm)] = m
            rets[("BTC", f"opt_{name}_iv{ivm}")] = r
            if ivm == 1.0:
                trials.append(("BTC", f"opt_{name}"))
                res[("BTC", f"opt_{name}")] = m
        # 2021+ only (real DVOL, no calibration)
    real = {}
    for name in ("put_always", "put_trend", "cc_trend"):
        kind = "put" if name.startswith("put") else "cc"
        r, _ = option_sim(btc, iv_base, rf, None if name == "put_always" else gate, kind, start="2021-04-01")
        real[name] = metrics(r)
    real["shares_" + ROBUST] = metrics(shares_r.loc["2021-04-01":])
    real["hold"] = metrics(rets[("BTC", "hold")].loc["2021-04-01":])
    opt_ci = {}
    for name in ("put_trend", "put_always", "cc_trend"):
        a = shares_r.to_numpy()
        b = rets[("BTC", f"opt_{name}_iv1.0")].reindex(shares_r.index).fillna(0).to_numpy()
        opt_ci[name] = (sharpe(b) - sharpe(a), *boot_diff(a, b, rng))

    # ---- report
    L = ["# Crypto trend via spot ETFs (IBIT/ETHA) + BTC option overlays", "",
         f"Data to {END} (holdout after it never loaded). NYSE trading days only. Generated {datetime.now():%Y-%m-%d %H:%M}.",
         "", "VERDICT_HERE", ""]
    for u in UNIVERSES:
        h = res[(u, "hold")]
        L += [f"## {u}, {h['start']} to {h['end']}", "",
              "| rule | CAGR | vol | Sharpe | maxDD | worst month | %mo>0 | avg exposure | turnover/yr |",
              "|---|---|---|---|---|---|---|---|---|",
              f"| buy & hold | {pct(h['cagr'])} | {pct(h['vol'])} | {h['sharpe']:.2f} | {pct(h['maxdd'])} | "
              f"{pct(h['worst_month'])} | {pct(h['pos_months'])} | 100% | - |"]
        for (uu, k) in trials:
            if uu != u or k.startswith("opt_"):
                continue
            m = res[(u, k)]
            mark = " **(default)**" if k == ROBUST else ""
            L.append(f"| {k}{mark} | {pct(m['cagr'])} | {pct(m['vol'])} | {m['sharpe']:.2f} | {pct(m['maxdd'])} | "
                     f"{pct(m['worst_month'])} | {pct(m['pos_months'])} | {pct(m['avg_expo'])} | {m['turnover']:.1f}x |")
        lm = res[(u, ROBUST + "|lag1")]
        L += ["", f"Default with a 1-bar lag (trade the same day the signal closes): CAGR {pct(lm['cagr'])}, "
                  f"Sharpe {lm['sharpe']:.2f}, maxDD {pct(lm['maxdd'])}.", ""]
        m, picks, _, md, mh = wf[u]
        bm = res[(u, ROBUST + "|band10")]
        L += [f"Default with a 10-point no-trade band (what a shadow would do): CAGR {pct(bm['cagr'])}, "
              f"Sharpe {bm['sharpe']:.2f}, maxDD {pct(bm['maxdd'])}, turnover {bm['turnover']:.1f}x/yr.", ""]
        L += [f"Walk-forward (each year uses the best in-sample Sharpe of the 33 rules, then trades the next year "
              f"out of sample), {m['start']} to {m['end']}: CAGR {pct(m['cagr'])}, Sharpe {m['sharpe']:.2f}, "
              f"maxDD {pct(m['maxdd'])}. Picks: " + ", ".join(f"{y}: {k}" for y, k in picks.items()),
              "", f"Same window: default CAGR {pct(md['cagr'])}, Sharpe {md['sharpe']:.2f}, maxDD {pct(md['maxdd'])}; "
              f"hold CAGR {pct(mh['cagr'])}, Sharpe {mh['sharpe']:.2f}, maxDD {pct(mh['maxdd'])}.", ""]
        L += ["| test | Sharpe minus hold | 95% CI (21-day block bootstrap) | deflated Sharpe prob. (66 trials) |",
              "|---|---|---|---|"]
        for (uu, k), s in stat.items():
            if uu == u:
                L.append(f"| {k}{' (in-sample best)' if s['best'] else ''} | {s['d_sharpe']:+.2f} | "
                         f"{s['lo']:+.2f} .. {s['hi']:+.2f} | {s['dsr']:.2f} |")
        L += ["", "Per year (default vs hold): " + ", ".join(
            f"{y}: {pct(res[(u, ROBUST)]['per_year'].get(y))} vs {pct(h['per_year'].get(y))}" for y in sorted(h["per_year"])), ""]
    L += [f"## BTC options (IBIT-sized), {EVAL_START['BTC']} to {END}; 30 DTE, hold to expiry unless noted", "",
          f"IV = Deribit DVOL from 2021-03; before that 21d RV x {ratio:.2f} (median DVOL/RV). Gate = 8-signal "
          "trend ensemble >= 0.5 (same signal family as the default).", "",
          "| variant | IV x | CAGR | vol | Sharpe | maxDD | worst month | cycles |", "|---|---|---|---|---|---|---|---|"]
    sm = res[("BTC", ROBUST)]
    L.append(f"| shares, default trend rule (reference) | - | {pct(sm['cagr'])} | {pct(sm['vol'])} | {sm['sharpe']:.2f} | "
             f"{pct(sm['maxdd'])} | {pct(sm['worst_month'])} | - |")
    for (name, ivm), m in sorted(opt_rows.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        L.append(f"| {name} | {ivm} | {pct(m['cagr'])} | {pct(m['vol'])} | {m['sharpe']:.2f} | {pct(m['maxdd'])} | "
                 f"{pct(m['worst_month'])} | {m['cycles']} |")
    L += ["", "Sharpe vs the shares default (IV x 1.0), 95% CI: " + "; ".join(
        f"{k} {d:+.2f} ({lo:+.2f} .. {hi:+.2f})" for k, (d, lo, hi) in opt_ci.items()), "",
          "Real DVOL only (2021-04 to 2025-11-21):", "", "| variant | CAGR | Sharpe | maxDD |", "|---|---|---|---|"]
    for k, m in real.items():
        L.append(f"| {k} | {pct(m['cagr'])} | {m['sharpe']:.2f} | {pct(m['maxdd'])} |")
    L += ["", "## Caveats", "",
          "- Proxy, not ETF prices: IBIT exists since 2024-01, ETHA since 2024-07. Yahoo crypto closes are 00:00 UTC; "
          "the 2-bar lag avoids using a close the ETF could not trade on. Weekend moves land in Monday's return.",
          "- Black-Scholes without skew; IBIT options trade since 2024-11 and their IV is close to but not equal to "
          "DVOL. Spreads on far strikes can be wider than 5% of premium.",
          "- 2015-2025 contains three crypto bear markets; trend rules shine there. In a steady bull run they lag holding.",
          "- Survivorship: BTC and ETH are the two survivors of their era; this does not generalise to other coins.",
          f"- Multiple testing: {len(trials)} trials registered. Use the deflated Sharpe and walk-forward rows, not the best row."]
    (OUT / "REPORT.md").write_text("\n".join(L), encoding="utf-8")
    summary = {f"{u}|{k}": {kk: vv for kk, vv in m.items() if kk != "per_year"} for (u, k), m in res.items()}
    (OUT / "summary.json").write_text(json.dumps({"results": summary, "dvol_rv_ratio": ratio,
                                                  "walkforward": {u: wf[u][0] for u in wf}}, indent=1, default=str))
    # registry
    if "--no-register" in sys.argv:
        print("\n".join(L))
        return 0
    if REG.exists():
        shutil.copy(REG, REG.with_name(f"trials_registry.backup_{datetime.now():%Y%m%dT%H%M%S}.csv"))
    t0 = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    for (u, k) in trials:
        m = res[(u, k)]
        key = f"crypto_trend|{u}|{k}"
        rows.append([t0, "crypto_trend", key, hashlib.sha1(key.encode()).hexdigest()[:10], u, m["start"], m["end"],
                     round(m["cagr"], 4), round(m["sharpe"], 3), round(m["maxdd"], 4),
                     round(m.get("turnover", m.get("cycles", 0)), 2),
                     "Yahoo proxy on NYSE days, 2-bar lag, ETF costs; options BS w/ DVOL" if k.startswith("opt_")
                     else "Yahoo proxy on NYSE days, 2-bar lag, 5bp/side + 0.25%/yr fee"])
    with open(REG, "a", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)
    print("\n".join(L))
    print("registered", len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
