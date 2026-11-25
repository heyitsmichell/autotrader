"""Options strategy research: modelled (Black-Scholes) backtests, 2011-01 -> 2025-11-21.

Locked holdout: NO data after 2025-11-23 (hard clip at END = 2025-11-21).
Pricer is imported read-only from scripts/options_entry_backtest.py.
Run: .venv/Scripts/python.exe scripts/options_research.py
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import options_entry_backtest as oeb  # noqa: E402  (read-only reuse of the BS pricer)

OUT = ROOT / "data" / "experiments" / "options_research"
RAW = OUT / "raw"
REG = ROOT / "data" / "experiments" / "trials_registry.csv"
START, END = date(2011, 1, 3), date(2025, 11, 21)
HOLDOUT = date(2025, 11, 23)
ACCOUNT = 25000.0
COMM = 0.65                      # $ per contract per side
SPREAD_RT = {"SPY": 0.03, "STK": 0.06}   # round trip, % of premium; half paid per side
STOCK_SLIP = 0.0005


# ------------------------------------------------------------------ pricing
def bs_call(S, K, T, sig, r=0.0):
    return oeb.bs_call(S, K, T, sig, r)


def bs_put(S, K, T, sig, r=0.0):
    if T <= 0:
        return max(K - S, 0.0)
    return bs_call(S, K, T, sig, r) - S + K * math.exp(-r * T)


def strike_for_put_delta(S, T, sig, d, r=0.0):
    """Strike whose put |delta| = d (call delta = 1-d)."""
    return oeb.strike_for_delta(S, T, sig, 1.0 - d, r)


def strike_for_call_delta(S, T, sig, d, r=0.0):
    return oeb.strike_for_delta(S, T, sig, d, r)


# ------------------------------------------------------------------ data
def read_px(path: Path, col="Close"):
    out = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                d = datetime.strptime(row["Date"][:10], "%Y-%m-%d").date()
                out[d] = float(row[col])
            except (ValueError, KeyError):
                continue
    return {d: v for d, v in out.items() if d <= END}


def load_bars(syms):
    from app.backtest.data import load_csv_dir
    bars = {}
    for d in (ROOT / "data" / "bars_yahoo_ext", ROOT / "data" / "bars_yahoo"):
        miss = [s for s in syms if s not in bars]
        for s, rows in load_csv_dir(d, miss).items():
            if rows:
                bars[s] = [b for b in rows if b["time"].date() <= END]
    return bars


class Mkt:
    """SPY-calendar market data."""

    def __init__(self):
        bars = load_bars(["SPY", "QQQ", "IWM"])
        self.bars = bars
        self.dates = [b["time"].date() for b in bars["SPY"] if START <= b["time"].date() <= END]
        self.idx = {d: i for i, d in enumerate(self.dates)}
        self.close = {s: self._series(bars[s]) for s in bars}
        vix = read_px(RAW / "VIX.csv")
        irx = read_px(RAW / "IRX.csv")
        self.vix = self._ffill(vix)
        self.rf = self._ffill(irx, scale=0.01)
        spy = np.array(self.close["SPY"])
        self.rv = {s: self._rv(np.array(self.close[s]), 20) for s in bars}
        self.rv60 = {s: self._rv(np.array(self.close[s]), 60) for s in bars}
        self.spy = spy

    def _series(self, rows):
        m = {b["time"].date(): b["close"] for b in rows}
        out, last = [], None
        for d in self.dates:
            last = m.get(d, last)
            out.append(last)
        return out

    def _ffill(self, m, scale=1.0):
        out, last = [], None
        for d in self.dates:
            last = m.get(d, last)
            out.append(None if last is None else last * scale)
        first = next(v for v in out if v is not None)
        return [first if v is None else v for v in out]

    @staticmethod
    def _rv(c, n):
        out = np.full(len(c), np.nan)
        lr = np.diff(np.log(c))
        for i in range(n, len(c)):
            out[i] = lr[i - n:i].std(ddof=1) * math.sqrt(252)
        return out

    def iv(self, sym, i):
        """Implied vol: VIX for SPY; VIX scaled by the 60d RV ratio for QQQ/IWM."""
        v = self.vix[i] / 100.0
        if sym == "SPY":
            return v
        ratio = self.rv60[sym][i] / self.rv60["SPY"][i]
        if not np.isfinite(ratio):
            ratio = 1.0
        return v * float(min(max(ratio, 0.6), 2.0))


# ------------------------------------------------------------------ cycle engine
def expiry_idx(mk: Mkt, o: int, dte=30):
    target = mk.dates[o] + timedelta(days=dte)
    for j in range(o + 1, len(mk.dates)):
        if mk.dates[j] >= target:
            return j, False
    return len(mk.dates) - 1, True


def growth(mk: Mkt, o: int):
    """cumulative cash growth factors from o (G[o]=1)."""
    return None


class Acct:
    """Cash + shares account with at most one short/long option leg per cycle."""

    def __init__(self, mk, sym, label, spread_rt, trades):
        self.mk, self.sym, self.label = mk, sym, label
        self.spread = spread_rt
        self.trades = trades
        self.eq = [np.nan] * len(mk.dates)
        self.c = ACCOUNT
        self.sh = 0.0

    def cycle(self, o, x, opt=None):
        """Run cycle o->x. opt=dict(kind put/call, side short/long, K, qty, iv_open, strike_delta).
        Returns (assigned, exit_reason). Updates cash/shares and eq path."""
        mk = self.mk
        S = mk.close[self.sym]
        base_c, sh = self.c, self.sh
        exp_date = mk.dates[x] if opt is None else opt["exp"]
        prem_open = prem_cash = 0.0
        if opt:
            T = max((exp_date - mk.dates[o]).days, 1) / 365.0
            pr = bs_put if opt["kind"] == "put" else bs_call
            prem_open = pr(S[o], opt["K"], T, opt["iv_open"], mk.rf[o])
            n_ct = opt["qty"] * (10 if self.sym == "SPY" else 1) / 100.0   # SPYM-sized contracts for SPY
            cost = 0.5 * self.spread * prem_open * opt["qty"] + COMM * n_ct
            sgn = 1.0 if opt["side"] == "short" else -1.0
            prem_cash = sgn * prem_open * opt["qty"] - cost  # cash received (negative = paid)
        g = 1.0
        for t in range(o, x + 1):
            if t > o:
                g *= math.exp(mk.rf[t - 1] * (mk.dates[t] - mk.dates[t - 1]).days / 365.0)
            if t == o and o != 0 and not np.isnan(self.eq[o]):
                pass
            val = base_c * g + sh * S[t]
            if opt:
                if t == x:
                    mark = (max(opt["K"] - S[t], 0.0) if opt["kind"] == "put" else max(S[t] - opt["K"], 0.0))
                else:
                    T = max((exp_date - mk.dates[t]).days, 0) / 365.0
                    pr = bs_put if opt["kind"] == "put" else bs_call
                    mark = pr(S[t], opt["K"], T, mk.iv(self.sym, t), mk.rf[t])
                sgn = 1.0 if opt["side"] == "short" else -1.0
                val += prem_cash - sgn * mark * opt["qty"]
            if t > o or np.isnan(self.eq[t]):
                self.eq[t] = val
        # settle
        self.c = base_c * g
        assigned = False
        if opt:
            sgn = 1.0 if opt["side"] == "short" else -1.0
            if opt["kind"] == "put":
                intr = max(opt["K"] - S[x], 0.0)
            else:
                intr = max(S[x] - opt["K"], 0.0)
            self.c += prem_cash
            if opt.get("physical") and intr > 0:
                if opt["kind"] == "put" and opt["side"] == "short":      # assigned: buy shares at K
                    self.c -= opt["qty"] * opt["K"]
                    self.sh += opt["qty"]
                    assigned = True
                elif opt["kind"] == "call" and opt["side"] == "short":   # called away: sell at K
                    self.c += opt["qty"] * opt["K"]
                    self.sh -= opt["qty"]
                    assigned = True
            else:
                self.c -= sgn * intr * opt["qty"]
            pnl = prem_cash - sgn * intr * opt["qty"]
            coll = opt["K"] * opt["qty"] if opt["kind"] == "put" else S[o] * opt["qty"]
            self.trades.append(dict(
                strategy=self.label, symbol=self.sym, open_date=mk.dates[o], close_date=mk.dates[x],
                option_type=opt["kind"], side="sell" if opt["side"] == "short" else "buy",
                strike=round(opt["K"], 2), dte=(exp_date - mk.dates[o]).days,
                iv_open=round(opt["iv_open"], 4), premium_open=round(prem_open, 4),
                premium_close=round(intr, 4), ret_on_capital=round(pnl / coll, 5),
                exit_reason=opt.get("reason", "expiry") + ("_assigned" if assigned else "")))
        return assigned


def rets_from_eq(eq, start_i):
    e = np.array(eq, float)
    e = e[start_i:]
    return np.diff(e) / e[:-1], start_i + 1


def _chain(mk, start_i=0, dte=30):
    """Yield (o, x) rolling cycles of ~dte calendar days."""
    o = start_i
    while o < len(mk.dates) - 1:
        x, end = expiry_idx(mk, o, dte)
        yield o, x
        if end:
            return
        o = x


def run_putwrite(mk, sym, label, delta=0.30, vrp_filter=False, spread_rt=0.03, trades=None):
    trades = trades if trades is not None else []
    a = Acct(mk, sym, label, spread_rt, trades)
    S = mk.close[sym]
    a.eq[0] = ACCOUNT
    skipped = n = 0
    for o, x in _chain(mk):
        n += 1
        iv = mk.iv(sym, o)
        if vrp_filter and not (iv - mk.rv["SPY"][o] > 0):
            skipped += 1
            a.cycle(o, x, None)
            continue
        exp = mk.dates[x]
        T = max((exp - mk.dates[o]).days, 1) / 365.0
        K = strike_for_put_delta(S[o], T, iv, delta, mk.rf[o])
        a.cycle(o, x, dict(kind="put", side="short", K=K, qty=a.c / K, iv_open=iv, exp=exp))
    return a, dict(cycles=n, skipped=skipped)


def run_covered_call(mk, sym, label, delta=0.30, spread_rt=0.03, trades=None):
    trades = trades if trades is not None else []
    a = Acct(mk, sym, label, spread_rt, trades)
    S = mk.close[sym]
    a.eq[0] = ACCOUNT
    a.sh, a.c = ACCOUNT / S[0], 0.0
    for o, x in _chain(mk):
        iv = mk.iv(sym, o)
        exp = mk.dates[x]
        T = max((exp - mk.dates[o]).days, 1) / 365.0
        K = strike_for_call_delta(S[o], T, iv, delta, mk.rf[o])
        a.cycle(o, x, dict(kind="call", side="short", K=K, qty=a.sh, iv_open=iv, exp=exp))
    return a, {}


def run_wheel(mk, sym, label, delta=0.30, spread_rt=0.03, trades=None):
    trades = trades if trades is not None else []
    a = Acct(mk, sym, label, spread_rt, trades)
    S = mk.close[sym]
    a.eq[0] = ACCOUNT
    basis = None
    for o, x in _chain(mk):
        iv = mk.iv(sym, o)
        exp = mk.dates[x]
        T = max((exp - mk.dates[o]).days, 1) / 365.0
        if a.sh <= 1e-9:
            K = strike_for_put_delta(S[o], T, iv, delta, mk.rf[o])
            if a.assigned_basis if False else False:
                pass
            asg = a.cycle(o, x, dict(kind="put", side="short", K=K, qty=a.c / K, iv_open=iv, exp=exp, physical=True))
            if asg:
                basis = K
        else:
            K = max(strike_for_call_delta(S[o], T, iv, delta, mk.rf[o]), basis)
            asg = a.cycle(o, x, dict(kind="call", side="short", K=K, qty=a.sh, iv_open=iv, exp=exp, physical=True))
            if asg:
                basis = None
    return a, {}


def run_protective(mk, sym, label, otm=0.05, spread_rt=0.03, trades=None):
    trades = trades if trades is not None else []
    a = Acct(mk, sym, label, spread_rt, trades)
    S = mk.close[sym]
    a.eq[0] = ACCOUNT
    a.sh, a.c = ACCOUNT / S[0], 0.0
    for o, x in _chain(mk):
        iv = mk.iv(sym, o)
        exp = mk.dates[x]
        a.cycle(o, x, dict(kind="put", side="long", K=S[o] * (1 - otm), qty=a.sh, iv_open=iv, exp=exp))
    return a, {}


def run_buyhold(mk, sym):
    S = np.array(mk.close[sym])
    eq = ACCOUNT * S / S[0]
    return list(eq)


# ------------------------------------------------------------------ single-stock signal CSP
def signal_trades(mk, syms):
    oeb.START, oeb.END = START, END
    from app.config.loader import load_settings
    from app.backtest.signals import build_features
    settings = load_settings()
    bars = load_bars(syms + ["SPY"])
    feats = {s: build_features(b) for s, b in bars.items()}
    have = [s for s in syms if s in bars]
    return have, bars, oeb.simulate_stock_trades(settings, have, bars, feats)


def run_signal_csp(mk, bars, trades, label, notional=2500.0, mgmt="hold", delta=0.30, dte=30, stock_mode=False):
    """CSP (or stock, stock_mode) on each signal. $notional per trade; daily MTM; cash earns rf on 25k."""
    n = len(mk.dates)
    pnl = np.zeros(n)                      # cumulative pnl series built as daily increments
    out_rows = []
    open_notional = np.zeros(n)
    taken = skipped = 0
    for tr in sorted(trades, key=lambda t: bars[t["sym"]][t["entry_i"]]["time"]):
        b = bars[tr["sym"]]
        closes = [x["close"] for x in b]
        e = tr["entry_i"]
        dts = [x["time"].date() for x in b]
        if dts[e] < START or dts[e] > END:
            continue
        S0 = tr["entry_px"]
        if stock_mode:
            ex = tr["exit_i"]
            sh = notional / S0
            last_end = ex
        else:
            sigma0 = oeb.iv_proxy(closes, tr["sig_i"])
            r0 = mk.rf[mk.idx[dts[e]]] if dts[e] in mk.idx else 0.04
            T0 = dte / 365.0
            K = strike_for_put_delta(S0, T0, sigma0, delta, r0)
            prem0 = bs_put(S0, K, T0, sigma0, r0)
            sh = notional / K
            exp_date = dts[e] + timedelta(days=dte)
            ex = next((j for j in range(e + 1, len(b)) if dts[j] >= exp_date), len(b) - 1)
        # capacity check: concurrent collateral <= account
        ie = mk.idx.get(dts[e])
        if ie is None:
            continue
        # Mark each day
        daily = {}
        reason = "expiry"
        if stock_mode:
            prev = S0 * (1 + STOCK_SLIP)
            cost_open = 0.0
            for j in range(e, ex + 1):
                px = closes[j] if j != ex else tr["stock_exit_px"] * (1 - STOCK_SLIP)
                daily[dts[j]] = sh * (px - prev)
                prev = px
            reason = tr["stock_reason"]
            prem_cl = tr["stock_exit_px"]
            ret_cap = (tr["stock_exit_px"] * (1 - STOCK_SLIP) / (S0 * (1 + STOCK_SLIP)) - 1)
        else:
            spread = SPREAD_RT["STK"]
            n_ct = sh / 100.0
            cost_open = 0.5 * spread * prem0 * sh + COMM * n_ct
            prev_val = prem0
            daily[dts[e]] = -cost_open
            exit_j, exit_val = ex, None
            for j in range(e, ex + 1):
                if j == ex:
                    mark = max(K - closes[j], 0.0)
                else:
                    Tj = max((exp_date - dts[j]).days, 0) / 365.0
                    mark = bs_put(closes[j], K, Tj, oeb.iv_proxy(closes, j), r0)
                if mgmt == "p50" and j > e and j < ex and mark <= 0.5 * prem0:
                    cost_cl = 0.5 * spread * mark * sh + COMM * n_ct
                    daily[dts[j]] = daily.get(dts[j], 0.0) - sh * (mark - prev_val) - cost_cl
                    exit_j, exit_val, reason = j, mark, "take_profit_50"
                    break
                daily[dts[j]] = daily.get(dts[j], 0.0) - sh * (mark - prev_val)
                prev_val = mark
                exit_val = mark
            ex = exit_j
            prem_cl = exit_val
            ret_cap = (prem0 - exit_val) / K - cost_open / (sh * K)
        # capacity (collateral) accounting
        d0, d1 = mk.idx.get(dts[e]), mk.idx.get(dts[ex])
        if d0 is None:
            continue
        d1 = d1 if d1 is not None else n - 1
        if open_notional[d0:d1 + 1].max() + notional > ACCOUNT:
            skipped += 1
            continue
        open_notional[d0:d1 + 1] += notional
        taken += 1
        for d, v in daily.items():
            if d in mk.idx:
                pnl[mk.idx[d]] += v
        out_rows.append(dict(
            strategy=label, symbol=tr["sym"], open_date=dts[e], close_date=dts[ex],
            option_type="stock" if stock_mode else "put", side="buy" if stock_mode else "sell",
            strike=round(S0 if stock_mode else K, 2), dte="" if stock_mode else (dts[ex] - dts[e]).days,
            iv_open="" if stock_mode else round(sigma0, 4),
            premium_open=round(S0, 4) if stock_mode else round(prem0, 4),
            premium_close=round(prem_cl, 4), ret_on_capital=round(ret_cap, 5), exit_reason=reason))
    eq = np.zeros(n)
    c = ACCOUNT
    for i in range(n):
        if i > 0:
            c *= math.exp(mk.rf[i - 1] * (mk.dates[i] - mk.dates[i - 1]).days / 365.0)
        eq[i] = c + pnl[: i + 1].sum()
    return list(eq), out_rows, dict(taken=taken, skipped=skipped)


# ------------------------------------------------------------------ metrics
EPISODES = {"2015-08": ("2015-07-31", "2015-09-30"), "2018-02": ("2018-01-25", "2018-04-30"),
            "2020-03": ("2020-02-18", "2020-04-30"), "2022": ("2022-01-01", "2022-12-31")}


def metrics(mk, eq, name):
    e = np.array(eq, float)
    ok = ~np.isnan(e)
    e = e[ok]
    d = [d for d, k in zip(mk.dates, ok) if k]
    r = np.diff(e) / e[:-1]
    yrs = (d[-1] - d[0]).days / 365.25
    cagr = (e[-1] / e[0]) ** (1 / yrs) - 1
    sharpe = float(r.mean() / r.std(ddof=1) * math.sqrt(252)) if r.std() > 0 else 0.0
    peak = np.maximum.accumulate(e)
    mdd = float((e / peak - 1).min())
    ym, py = {}, {}
    for i in range(1, len(e)):
        ym.setdefault((d[i].year, d[i].month), []).append(1 + r[i - 1])
        py.setdefault(d[i].year, []).append(1 + r[i - 1])
    mon = {k: float(np.prod(v) - 1) for k, v in ym.items()}
    wk = min(mon, key=mon.get)
    eps = {}
    for k, (a, b) in EPISODES.items():
        a, b = date.fromisoformat(a), date.fromisoformat(b)
        seg = [x for x, dd in zip(e, d) if a <= dd <= b]
        if len(seg) > 1:
            sg = np.array(seg)
            eps[k] = float((sg / np.maximum.accumulate(sg) - 1).min())
    return dict(name=name, start=str(d[0]), end=str(d[-1]), cagr=float(cagr), sharpe=sharpe, maxdd=mdd,
                worst_month=mon[wk], worst_month_label=f"{wk[0]}-{wk[1]:02d}",
                per_year={str(y): float(np.prod(v) - 1) for y, v in sorted(py.items())}, episodes_maxdd=eps), r, d[1:]


def index_eq(mk, path):
    m = read_px(path, "Adj Close") if False else read_px(path, "Close")
    out, last = [], None
    for d in mk.dates:
        last = m.get(d, last)
        out.append(last)
    first = next(v for v in out if v is not None)
    return [ACCOUNT * (first if v is None else v) / first for v in out]


# ------------------------------------------------------------------ main
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    mk = Mkt()
    assert max(mk.dates) < HOLDOUT, "holdout violated"
    results, series, all_trades = {}, {}, []
    notes = {}

    def add(name, eq, trades=None, extra=None, ntr=None):
        m, r, dd = metrics(mk, eq, name)
        m["n_trades"] = ntr if ntr is not None else len(trades or [])
        if extra:
            m.update(extra)
        results[name] = m
        series[name] = (dd, r)
        if trades:
            all_trades.extend(trades)

    # benchmarks
    add("SPY_buyhold", run_buyhold(mk, "SPY"), ntr=0)
    add("QQQ_buyhold", run_buyhold(mk, "QQQ"), ntr=0)
    add("IWM_buyhold", run_buyhold(mk, "IWM"), ntr=0)
    for nm, f in (("PUT_index_CBOE(yf)", "PUT.csv"),):
        add(nm, index_eq(mk, RAW / f), ntr=0)

    for sym in ("SPY", "QQQ", "IWM"):
        t = []
        a, ex = run_putwrite(mk, sym, f"a_putwrite30_{sym}", trades=t)
        add(f"a_putwrite30_{sym}", a.eq, t, ex)
        t = []
        a, _ = run_covered_call(mk, sym, f"b_covcall30_{sym}", trades=t)
        add(f"b_covcall30_{sym}", a.eq, t)
    t = []
    a, _ = run_wheel(mk, "SPY", "c_wheel30_SPY", trades=t)
    add("c_wheel30_SPY", a.eq, t)
    t = []
    a, ex = run_putwrite(mk, "SPY", "d_putwrite30_vrp_pos_SPY", vrp_filter=True, trades=t)
    add("d_putwrite30_vrp_pos_SPY", a.eq, t, ex)
    for otm in (0.05, 0.10):
        t = []
        a, _ = run_protective(mk, "SPY", f"f_protput_{int(otm*100)}otm_SPY", otm=otm, trades=t)
        add(f"f_protput_{int(otm*100)}otm_SPY", a.eq, t)

    # (e) signals as CSP
    from app.config.loader import load_settings
    syms = list(load_settings().options_allowed_underlyings)
    have, bars, sigs = signal_trades(mk, syms)
    notes["signal_universe"] = have
    notes["n_signals"] = len(sigs)
    for label, kw in (("e_signal_csp30_hold", dict(mgmt="hold")), ("e_signal_csp30_tp50", dict(mgmt="p50")),
                      ("e_signal_STOCK_samenotional", dict(stock_mode=True))):
        eq, rows, ex = run_signal_csp(mk, bars, sigs, label, **kw)
        add(label, eq, rows, ex)

    # outputs
    for name, (dd, r) in series.items():
        with gzip.open(OUT / f"daily_{name}.csv.gz", "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "ret"])
            for x, y in zip(dd, r):
                w.writerow([str(x), f"{y:.6f}"])
    cols = ["strategy", "symbol", "open_date", "close_date", "option_type", "side", "strike", "dte", "iv_open",
            "premium_open", "premium_close", "ret_on_capital", "exit_reason"]
    with open(OUT / "trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(all_trades)
    prior = sum(1 for _ in open(REG)) - 1
    summ = dict(generated=datetime.now().isoformat(timespec="seconds"), window=f"{START}..{END}", notes=notes,
                registry_rows_before=prior, results=results)
    (OUT / "summary.json").write_text(json.dumps(summ, indent=1))
    write_report(results, notes, prior)
    append_registry(results, prior)
    print("done", len(results), "variants; registry before", prior)


def pct(x):
    return f"{x*100:.1f}%"


def write_report(res, notes, prior):
    keys = list(res)
    L = ["# Options strategy research (modelled + real index benchmarks)", "", "VERDICT_PLACEHOLDER", ""]
    L += ["## Results 2011-01-03 to 2025-11-21 (account $25,000; Black-Scholes model)", "",
          "| strategy | CAGR | Sharpe | maxDD | worst month | trades |", "|---|---|---|---|---|---|"]
    for k in keys:
        m = res[k]
        L.append(f"| {k} | {pct(m['cagr'])} | {m['sharpe']:.2f} | {pct(m['maxdd'])} | "
                 f"{pct(m['worst_month'])} ({m['worst_month_label']}) | {m['n_trades']} |")
    L += ["", "## Max drawdown inside stress windows", "", "| strategy | 2015-08 | 2018-02 | 2020-03 | 2022 |", "|---|---|---|---|---|"]
    for k in keys:
        e = res[k]["episodes_maxdd"]
        L.append(f"| {k} | " + " | ".join(pct(e[x]) if x in e else "-" for x in EPISODES) + " |")
    yrs = sorted({y for m in res.values() for y in m["per_year"]})
    L += ["", "## Per-year returns", "", "| strategy | " + " | ".join(yrs) + " |", "|---|" + "---|" * len(yrs)]
    for k in keys:
        L.append(f"| {k} | " + " | ".join(pct(res[k]["per_year"][y]) if y in res[k]["per_year"] else "-" for y in yrs) + " |")
    L += ["", "## Assumptions and caveats", "",
          "- Black-Scholes, r = 13-week T-bill (^IRX), no dividends (adjusted-close prices include them in the stock leg only).",
          "- SPY: implied vol = VIX (flat, no skew; 30-delta puts are in reality priced a little above VIX, so put-write premium is somewhat understated, covered-call premium somewhat overstated). QQQ/IWM: VIX x (60d RV of the ETF / 60d RV of SPY), clipped 0.6-2.0: a proxy, not real QQQ/IWM implied vol.",
          "- Single stocks (e): IV = max(0.12, 1.15 x 20-day RV). Rough proxy; it understates the variance premium for short-vol strategies, so (e) is conservative for the put seller, but it has no earnings-IV jump or crush either.",
          "- Cycles roll every ~30 calendar days (not exchange expiry dates); cash-settled at expiry intrinsic, wheel is physical (assignment). Daily mark-to-market uses the day's VIX.",
          "- Sizing: fractional contracts, collateral = full strike notional (cash-secured, 100% of account in a/c/d). SPY at ~$600 needs $60k per contract: a $25k account would have to use SPYM (1/10 size) or a smaller underlying. Commission charged at SPYM-sized contract counts.",
          "- Costs: half of the round-trip bid-ask (3% of premium SPY/QQQ/IWM, 6% single stocks) per side when a trade is opened or closed early, plus $0.65/contract/side. Expiry settlement has no exit cost.",
          "- (e): $2,500 collateral per signal (10% of account), max concurrent collateral $25,000 (extra signals skipped), cash earns rf. Same signals/entries as options_entry_backtest (live signal_at, next-open entry). Stock comparison uses variant-d exits and the same notional. Option marks reuse the IV proxy daily.",
          "- BXM / BXMD: not obtainable (yfinance returns nothing; direct download from cdn.cboe.com was blocked by the environment). ^PUT from yfinance is used as the real put-write benchmark; the modelled covered call (b) stands in for BXM.",
          f"- Multiple testing: registry had {prior} trials before this run; {len(res)} rows (incl. 4 benchmarks) were added here (lab=options_research). Any Sharpe picked as best is inflated; deflate before trusting.",
          "- The locked holdout (after 2025-11-23) was not used; data hard-clipped at 2025-11-21."]
    (OUT / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


def append_registry(res, prior):
    rows = []
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for k, m in res.items():
        h = hashlib.sha1(f"options_research|{k}".encode()).hexdigest()[:10]
        uni = "options_allowed(18)" if k.startswith("e_") else ("SPY/QQQ/IWM" if "_buyhold" in k else k.split("_")[-1])
        rows.append([ts, "options_research", k, h, uni, m["start"], m["end"], round(m["cagr"], 4), round(m["sharpe"], 3),
                     round(m["maxdd"], 4), m["n_trades"],
                     f"BS-modelled, VIX/1.15xRV IV, 25k acct; deflation: {prior} prior trials + {len(res)} here"])
    with open(REG, "a", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows(rows)


if __name__ == "__main__":
    main()
