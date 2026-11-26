"""Catalog strategy implementations. Each fn(ctx, cfg) -> dict(gross, net, turn) as numpy arrays over ctx.P.index
(or a list of such dicts for fns that evaluate several configs in one pass, see MULTI)."""
from __future__ import annotations
import math
from statistics import NormalDist
import numpy as np
import pandas as pd
from .data import (Panel, sim, rsi, atr, topn_portfolio, run_events, month_end_flags, SLIP, SEC_FEE, SOCIAL, _read)

ND = NormalDist()


class Ctx:
    def __init__(self, P: Panel):
        self.P = P
        self.cache = {}
        self.T = len(P.index)
        self.Oa, self.Ca = P.O.values, P.C.values
        self.spy = P.spy["close"]
        self.reg200 = ((self.spy > self.spy.rolling(200).mean()) & self.spy.rolling(200).mean().notna()).values
        self.groups = None

    def f(self, key, fn):
        if key not in self.cache:
            self.cache[key] = fn()
        return self.cache[key]

    def ret1(self):
        return self.f("r1", lambda: self.P.C.pct_change(fill_method=None))

    def sma(self, n):
        return self.f(("sma", n), lambda: self.P.C.rolling(n, min_periods=n).mean())

    def atr14(self):
        return self.f("atr14", lambda: atr(self.P.H, self.P.L, self.P.C, 14))

    def gid(self):
        """Group ids: GICS Sub-Industry if >=4 members else Sector."""
        if self.groups is None:
            P = self.P
            cnt = pd.Series(P.industry).value_counts()
            lab = [P.industry[s] if cnt[P.industry[s]] >= 4 else P.sector[s] for s in P.stocks]
            self.groups = np.array(pd.factorize(pd.Series(lab))[0])
        return self.groups


def _out(W, ctx, O=None):
    g, n, t = sim(W, ctx.Oa if O is None else O)
    return dict(gross=g, net=n, turn=t)


# ----------------------------------------------------------------------------------------------- daily equity
def connors(ctx, c):
    P = ctx.P
    r = ctx.f(("rsi", c["rsi_period"]), lambda: rsi(P.C, c["rsi_period"]))
    ibs = ctx.f("ibs", lambda: (P.C - P.L) / (P.H - P.L).replace(0, np.nan))
    regime = ctx.reg200 | (P.vix.values < 32)
    cand = ((P.C > ctx.sma(200)) & (r < c["rsi_entry_threshold"]) & (ibs < c["ibs_threshold"])).values & regime[:, None]
    exit_sig = ((P.C > ctx.sma(c["exit_sma_period"])) | (ibs > 0.8)).values
    W = run_events(cand, r.values, exit_sig, 5, c["max_positions"], ctx.Ca, ctx.atr14().values, stop_mult=2.5)
    return _out(W, ctx)


def _beta(ctx, n=252):
    def fn():
        r1, sr = ctx.ret1(), ctx.spy.pct_change()
        ex, ey = r1.rolling(n, min_periods=n).mean(), sr.rolling(n, min_periods=n).mean()
        exy = r1.mul(sr, axis=0).rolling(n, min_periods=n).mean()
        return exy.sub(ex.mul(ey, axis=0)).div(sr.rolling(n, min_periods=n).var(ddof=0), axis=0)
    return ctx.f(("beta", n), fn)


def idio_resid_mom(ctx, c):
    P = ctx.P
    L, K = c["lookback_days"], c["skip_recent_days"]
    res = ctx.f("resid", lambda: ctx.ret1().sub(_beta(ctx).mul(ctx.spy.pct_change(), axis=0)))
    num = res.rolling(L - K, min_periods=L - K).sum().shift(K)
    sd = res.rolling(L - K, min_periods=L - K).std().shift(K)
    score = (num / sd).values
    elig = (P.C > ctx.sma(100)).values
    sec = [P.sector[s] for s in P.stocks]
    W = topn_portfolio(score, elig, c["top_n_holdings"], month_end_flags(P.index), sector=sec, sec_cap=c["sector_cap_pct"])
    return _out(W, ctx)


def week52(ctx, c):
    P = ctx.P
    hi = P.H.rolling(c["high_lookback_days"], min_periods=c["high_lookback_days"]).max()
    prox = (P.C / hi)
    elig = ((prox >= c["min_proximity"]) & (P.C > ctx.sma(200))).values
    reb = (np.arange(ctx.T) % c["rebalance_days"]) == 0
    breadth = (prox >= 0.9).sum(axis=1).values
    scale = np.where(breadth < 30, 0.5, 1.0)
    W = topn_portfolio(prox.values, elig, c["top_n_holdings"], reb, scale=scale,
                       sector=[P.sector[s] for s in P.stocks], sec_cap=0.30)
    return _out(W, ctx)


def lowvol_quality(ctx, c):
    P = ctx.P
    w = c["vol_window_days"]
    semi = (ctx.ret1().clip(upper=0) ** 2).rolling(w, min_periods=w).mean() ** 0.5
    ret252 = P.C / P.C.shift(252) - 1
    elig = (ret252 > 0).values                     # PRICE-BASED quality proxy (no fundamentals in repo)
    lab = ["DEF" if P.sector[s] in ("Utilities", "Consumer Staples") else P.sector[s] for s in P.stocks]
    iv = (1.0 / semi.replace(0, np.nan) ** 2).values
    reb = (np.arange(ctx.T) % (21 * c["rebalance_months"])) == 0
    W = topn_portfolio(-semi.values, elig, c["top_n_stocks"], reb, sector=lab, sec_cap={"DEF": 0.20}, weights=iv)
    return _out(W, ctx)


def amihud_quality(ctx, c):
    P = ctx.P
    w = c["amihud_window_days"]
    illiq = (ctx.ret1().abs() / (P.C * P.V).replace(0, np.nan)).rolling(w, min_periods=w).mean()
    ret252 = P.C / P.C.shift(252) - 1
    q = {0.10: 0.3, 0.12: 0.4, 0.15: 0.5}[c["min_roe_pct"]]       # ROE not available: 12m-return pct-rank proxy
    elig = (ret252.rank(axis=1, pct=True) >= q).values
    reb = (np.arange(ctx.T) % c["rebalance_days"]) == 0
    W = topn_portfolio(illiq.values, elig, c["top_n_holdings"], reb)
    return _out(W, ctx)


def intra_industry_reversal(ctx, c):
    P = ctx.P
    g = ctx.gid()
    r5 = ctx.f("r5", lambda: (P.C / P.C.shift(5) - 1).values)
    def gmed():
        out = np.full_like(r5, np.nan)
        for k in np.unique(g):
            cols = np.where(g == k)[0]
            with np.errstate(all="ignore"):
                out[:, cols] = np.nanmedian(r5[:, cols], axis=1)[:, None]
        return out
    exc = r5 - ctx.f("gmed5", gmed)
    r3 = ctx.f(("rsi", 3), lambda: rsi(P.C, 3)).values
    cand = (exc < c["excess_return_threshold"]) & (r3 < c["rsi_filter"]) & (P.C > ctx.sma(150)).values
    W = run_events(cand, exc, exc >= 0, c["hold_days"], 10, ctx.Ca, ctx.atr14().values, stop_mult=2.5, group=g, group_cap=2)
    return _out(W, ctx)


def bellwether(ctx, c):
    P = ctx.P
    g = ctx.gid()
    r1 = ctx.ret1()
    z = (r1 / r1.rolling(60, min_periods=60).std().shift(1)).values
    dv = (P.C * P.V).rolling(252, min_periods=60).mean().fillna(-1).values
    T, M = z.shape
    cand = np.zeros((T, M), bool)
    for k in np.unique(g):
        cols = np.where(g == k)[0]
        if len(cols) < 4:
            continue
        lead = cols[np.argmax(dv[:, cols], axis=1)]
        zl = z[np.arange(T), lead]
        trig = zl > c["leader_sigma_min"]
        for j in cols:
            cand[:, j] = trig & (lead != j) & (z[:, j] < c["laggard_max_sigma"])
    cand &= np.isfinite(z)
    W = run_events(cand, z, np.zeros((T, M), bool), c["hold_days"], 10, ctx.Ca, ctx.atr14().values, stop_mult=2.0,
                   group=g, group_cap=3)
    return _out(W, ctx)


def overnight_reversal(ctx, c):
    P = ctx.P
    N = c["decile_count"]
    O, C = P.O, P.C
    dayret = (C / O - 1)
    spyd = (ctx.P.spy["close"] / ctx.P.spy["open"] - 1)
    adj = (dayret.sub(_beta(ctx, 126).mul(spyd, axis=0))).values
    dvol = (C * P.V).rolling(60, min_periods=60).mean()
    liq = dvol.rank(axis=1, ascending=False).values <= 100        # top-100 liquid S&P names as S&P100/NDX proxy
    cand = liq & (adj <= c["min_intraday_drop_pct"]) & np.isfinite(adj)
    key = np.where(cand, adj, np.inf)
    rank = key.argsort(axis=1).argsort(axis=1)
    sel = cand & (rank < N)
    with np.errstate(all="ignore"):
        G = np.zeros(C.shape); G[:-1] = O.values[1:] / C.values[:-1] - 1.0     # close t -> open t+1
    G = np.nan_to_num(np.clip(G, -0.5, 0.5))
    w = sel / N
    gross_t = (w * G).sum(1)
    exp = w.sum(1)
    net_t = gross_t - exp * (2 * SLIP + SEC_FEE)
    gross = np.zeros(ctx.T); net = np.zeros(ctx.T); turn = np.zeros(ctx.T)
    gross[1:], net[1:], turn[1:] = gross_t[:-1], net_t[:-1], 2 * exp[:-1]      # earned on the next morning
    return dict(gross=gross, net=net, turn=turn)


# ----------------------------------------------------------------------------------------------- ETF strategies
def turn_of_month(ctx, c):
    P = ctx.P
    tk = c["target_etf"]
    O, H, L, C = P.etf([tk])
    idx = P.index
    me = np.where(month_end_flags(idx))[0]
    W = np.zeros((ctx.T, 1))
    for i in me:
        e = i + c["entry_offset_days"] + 1
        x = i + c["exit_offset_days"]
        if x < ctx.T:
            W[e:x, 0] = 1.0
    scale = np.where(ctx.reg200, 1.0, c["bear_regime_scale"])
    W[:, 0] *= scale
    return _out(W, ctx, O.values)


def inv_vol_trend(ctx, c):
    P = ctx.P
    risky = ["SPY", "EFA", "TLT", "GLD", "DBC"]
    O, H, L, C = P.etf(risky + ["BIL"])
    r = C.pct_change(fill_method=None)
    vol = r.rolling(c["vol_lookback_days"], min_periods=c["vol_lookback_days"]).std() * math.sqrt(252)
    elig = (C[risky] > C[risky].rolling(c["trend_sma_days"], min_periods=c["trend_sma_days"]).mean()).values
    me = month_end_flags(P.index)
    T = ctx.T
    W = np.zeros((T, 6)); W[:, 5] = 1.0
    cur = W[0].copy(); cur_el = np.zeros(5, bool)
    rv = r[risky].values
    for t in range(T):
        if me[t] and t >= 260:
            el = elig[t] & np.isfinite(vol[risky].values[t])
            new = np.zeros(6)
            if el.any():
                iv = np.where(el, 1.0 / vol[risky].values[t], 0.0)
                w = iv / iv.sum()
                seg = rv[max(0, t - 59):t + 1][:, el]
                cov = np.cov(np.nan_to_num(seg).T).reshape(el.sum(), el.sum()) * 252
                ww = w[el]
                pv = math.sqrt(max(ww @ cov @ ww, 1e-12))
                s = min(1.0, c["target_vol_annual"] / pv)
                s20 = np.nan_to_num(rv[t - 19:t + 1][:, el])
                if el.sum() > 1:
                    cc = np.corrcoef(s20.T)
                    avg = (cc.sum() - el.sum()) / (el.sum() * (el.sum() - 1))
                    if avg > 0.65:
                        s = min(s, 0.6)
                new[:5] = w * s
            new[5] = 1.0 - new[:5].sum()
            if (np.abs(new - cur).max() > c["drift_band_pct"]) or (el != cur_el).any():
                cur, cur_el = new, el
        W[t] = cur
    return _out(W, ctx, O.values)


def vix_carry(ctx, c):
    P = ctx.P
    sv = _read(SOCIAL / "SVXY.csv")
    O = sv["open"].reindex(P.index).values[:, None]
    slope = ((P.vix3m - P.vix) / P.vix).values
    vix = P.vix.values
    sma50 = ctx.spy.rolling(50).mean().values
    spyc = ctx.spy.values
    jump = np.zeros(ctx.T); jump[1:] = vix[1:] / vix[:-1] - 1
    W = np.zeros((ctx.T, 1)); on = False
    for t in range(ctx.T):
        if np.isnan(O[t, 0]) and not on:
            continue
        if on:
            if slope[t] < c["exit_slope_threshold"] or jump[t] > 0.25 or slope[t] < 0:
                on = False
        elif slope[t] > c["min_contango_slope"] and vix[t] < c["max_vix_entry"] and spyc[t] > sma50[t]:
            on = True
        W[t, 0] = 1.0 if on else 0.0
    return _out(W, ctx, O)


# ----------------------------------------------------------------------------------------------- pairs
PAIRS = [("KO", "PEP"), ("V", "MA"), ("XOM", "CVX"), ("HD", "LOW"), ("MSFT", "AAPL"), ("JPM", "BAC"), ("GS", "MS"),
         ("UNP", "CSX"), ("COP", "EOG"), ("WMT", "COST"), ("MCD", "YUM"), ("PG", "CL"), ("AMT", "CCI"),
         ("DUK", "SO"), ("LIN", "APD"), ("TMO", "DHR"), ("ABT", "MDT"), ("MRK", "PFE")]


def _kalman(y, x, delta=1e-5, ve=1e-3):
    """1-state Kalman filter for the hedge ratio b_t in log(y) = b_t * log(x); returns the post-update spread
    y - b_t x (persistent, unlike the whitened innovation) and b_t."""
    n = len(y)
    th, Pc, Vw = 1.0, 1.0, delta
    sp = np.full(n, np.nan); b = np.full(n, np.nan)
    for t in range(n):
        if not (np.isfinite(y[t]) and np.isfinite(x[t])):
            continue
        R = Pc + Vw
        Q = x[t] * x[t] * R + ve
        e = y[t] - th * x[t]
        K = R * x[t] / Q
        th = th + K * e
        Pc = R - K * x[t] * R
        sp[t] = y[t] - th * x[t]
        b[t] = th
    return sp, b


def kalman_pairs(ctx, c):
    P = ctx.P
    lb = c["lookback_window"]
    pairs = [(a, b) for a, b in PAIRS if a in P.stocks and b in P.stocks]
    ci = {s: i for i, s in enumerate(P.stocks)}
    lc = np.log(P.C)
    spyrv = (ctx.spy.pct_change().rolling(20).std() * math.sqrt(252)).values
    W = np.zeros((ctx.T, len(P.stocks)))
    for a, b in pairs:
        key = ("kal", a, b, lb)
        def build():
            e, beta = _kalman(lc[a].values, lc[b].values)
            es = pd.Series(e)
            z = ((es - es.rolling(lb, min_periods=lb).mean()) / es.rolling(lb, min_periods=lb).std()).values
            phi = (es.rolling(lb, min_periods=lb).corr(es.shift(1))).values
            with np.errstate(all="ignore"):
                hl = np.where((phi > 0) & (phi < 1), -math.log(2) / np.log(phi), np.inf)
            return z, hl, beta
        z, hl, beta = ctx.f(key, build)
        pos = 0; ent_t = 0; hl_e = 0; hb = 1.0
        for t in range(ctx.T):
            if pos != 0:
                zt = z[t]
                done = (not np.isfinite(zt)) or abs(zt) > c["stop_z"] or (t - ent_t) > 2 * hl_e
                if pos == 1 and zt >= -c["exit_z"]:
                    done = True
                if pos == -1 and zt <= c["exit_z"]:
                    done = True
                if done:
                    pos = 0
            elif np.isfinite(z[t]) and 2 <= hl[t] <= 15 and spyrv[t] < 0.28 and np.isfinite(beta[t]):
                if z[t] < -c["entry_z"]:
                    pos, ent_t, hl_e, hb = 1, t, hl[t], float(np.clip(beta[t], 0.2, 3.0))
                elif z[t] > c["entry_z"]:
                    pos, ent_t, hl_e, hb = -1, t, hl[t], float(np.clip(beta[t], 0.2, 3.0))
            if pos != 0:
                W[t, ci[a]] += pos / len(pairs)
                W[t, ci[b]] -= pos * hb / len(pairs)
    return _out(W, ctx)


# ----------------------------------------------------------------------------------------------- PCA stat arb
def pca_stat_arb_multi(ctx, cfgs):
    """All cfgs share (pca_window_days, num_eigenvectors); evaluates every (entry_s_score, max_half_life) in one pass."""
    P = ctx.P
    w, k = cfgs[0]["pca_window_days"], cfgs[0]["num_eigenvectors"]
    cols = ctx.f("pca_univ", lambda: _pca_universe(ctx))
    R = ctx.ret1()[[P.stocks[i] for i in cols]].values
    T, N = R.shape
    machines = [dict(es=c["entry_s_score"], mh=c["max_half_life_days"], pos={}, W=np.zeros((T, len(P.stocks)), np.float32))
                for c in cfgs]
    maxpos = 10
    for t in range(w + 1, T):
        seg = R[t - w + 1:t + 1]
        ok = np.isfinite(seg).all(0)
        if ok.sum() < 60:
            continue
        ix = np.where(ok)[0]
        X = seg[:, ix]
        mu, sd = X.mean(0), X.std(0, ddof=1)
        sd[sd == 0] = 1
        Z = (X - mu) / sd
        cm = Z.T @ Z / (w - 1)
        lam, V = np.linalg.eigh(cm)
        Vk = V[:, -k:]
        absorb = lam[-1] / lam.sum() > 0.45
        Pm = Vk @ Vk.T
        res = Z - Z @ Pm
        Xc = np.cumsum(res, 0)
        x0, x1 = Xc[:-1], Xc[1:]
        m0, m1 = x0.mean(0), x1.mean(0)
        var0 = ((x0 - m0) ** 2).mean(0)
        cov = ((x0 - m0) * (x1 - m1)).mean(0)
        b = cov / np.where(var0 > 0, var0, np.nan)
        a = m1 - b * m0
        vr = ((x1 - a - b * x0) ** 2).mean(0)
        with np.errstate(all="ignore"):
            valid = (b > 0) & (b < 1)
            m = a / (1 - b)
            seq = np.sqrt(vr / (1 - b ** 2))
            s = (Xc[-1] - m) / seq
            hl = np.where(valid, -math.log(2) / np.log(b), np.inf)
        for mc in machines:
            pos = mc["pos"]
            for i in list(pos):
                sg, vec, et, hle = pos[i]
                j = np.searchsorted(ix, i)
                done = (j >= len(ix) or ix[j] != i)
                if not done:
                    sj = s[j]
                    done = (not np.isfinite(sj)) or abs(sj) > 3.25 or (t - et) > 2 * hle or (sg == 1 and sj >= -0.25) or (sg == -1 and sj <= 0.25)
                if done:
                    del pos[i]
            if not absorb and len(pos) < maxpos:
                good = np.where(valid & (hl >= 1.5) & (hl <= mc["mh"]) & (np.abs(s) > mc["es"]))[0]
                good = good[np.argsort(-np.abs(s[good]))]
                for j in good:
                    if len(pos) >= maxpos:
                        break
                    gi = ix[j]
                    if gi in pos:
                        continue
                    coef = -Pm[:, j].copy(); coef[j] += 1.0
                    coef = coef / sd[:]
                    coef = coef / np.abs(coef).sum()
                    pos[gi] = (-1 if s[j] > 0 else 1, (ix.copy(), coef.copy()), t, float(hl[j]))
            row = mc["W"][t]
            for i, (sg, (ixs, coef), et, hle) in pos.items():
                row[[cols[q] for q in ixs]] += (sg * coef / maxpos).astype(np.float32)
    outs = []
    for mc in machines:
        outs.append(_out(mc["W"], ctx))
        mc["W"] = None
    return outs


def _pca_universe(ctx):
    P = ctx.P
    full = P.C.notna().all(axis=0)
    dv = (P.C * P.V).loc[:, full].mean()
    top = dv.sort_values(ascending=False).index[:100]
    return [P.stocks.index(s) for s in top]


# ----------------------------------------------------------------------------------------------- modelled options
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _bs(S, K, T, sig, r, put):
    if T <= 1e-6:
        return max(K - S, 0.0) if put else max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if put:
        return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def _strike(S, T, sig, r, d, put):
    d1 = ND.inv_cdf(1 - d) if put else ND.inv_cdf(d)
    return S * math.exp(-d1 * sig * math.sqrt(T) + (r + 0.5 * sig * sig) * T)


def _adx(H, L, C, n=14):
    up, dn = H.diff(), -L.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0); mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([H - L, (H - C.shift()).abs(), (L - C.shift()).abs()], axis=1).max(axis=1)
    a = 1 / n
    atr_ = tr.ewm(alpha=a, adjust=False).mean()
    pdi = 100 * pd.Series(pdm, index=H.index).ewm(alpha=a, adjust=False).mean() / atr_
    mdi = 100 * pd.Series(mdm, index=H.index).ewm(alpha=a, adjust=False).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return dx.ewm(alpha=a, adjust=False).mean()


def iron_condor(ctx, c):
    """MODELLED premiums (Black-Scholes, flat IV = VIX/100, r=4%): not real fills. SPY only. One condor at a time,
    sized so max loss = 5% of equity; entry executes at the NEXT close; marks at closes."""
    P = ctx.P
    S = P.spy["close"].values
    vix = P.vix.values / 100.0
    idx = P.index
    vser = P.vix
    ivr = ((vser - vser.rolling(252).min()) / (vser.rolling(252).max() - vser.rolling(252).min()) * 100).values
    rv20 = (P.spy["close"].pct_change().rolling(20).std() * math.sqrt(252)).values
    adx = _adx(P.spy["high"], P.spy["low"], P.spy["close"]).values
    r = 0.04
    eq = 1.0
    T = ctx.T
    ret = np.zeros(T); turn = np.zeros(T); gross = np.zeros(T)
    pos = None; pending = False
    SPR, COMM = 0.03, 0.65 * 8 / 100.0     # 3% round-trip of premium; commissions per condor per share-unit (8 leg-sides)
    def value(p, t):
        sg = vix[t]
        T_ = max((p["exp"] - idx[t]).days, 0) / 365.0
        v = (_bs(S[t], p["kps"], T_, sg, r, True) - _bs(S[t], p["kpl"], T_, sg, r, True)
             + _bs(S[t], p["kcs"], T_, sg, r, False) - _bs(S[t], p["kcl"], T_, sg, r, False))
        return v, T_, sg
    for t in range(260, T):
        if pos is not None:
            v, T_, sg = value(pos, t)
            pnl = pos["qty"] * (pos["last"] - v)
            pos["last"] = v
            dpsd = abs(_ncdf((math.log(S[t] / pos["kps"]) + (r + .5 * sg * sg) * T_) / (sg * math.sqrt(max(T_, 1e-6)))) - 1) if T_ > 0 else 0
            dcsd = _ncdf((math.log(S[t] / pos["kcs"]) + (r + .5 * sg * sg) * T_) / (sg * math.sqrt(max(T_, 1e-6)))) if T_ > 0 else 0
            dte = (pos["exp"] - idx[t]).days
            loss = v - pos["credit"]
            close = (v <= (1 - c["take_profit_pct"]) * pos["credit"] or loss >= 1.5 * pos["credit"]
                     or max(dpsd, dcsd) > 0.35 or dte <= c["exit_dte"])
            cost = 0.0
            if close:
                cost = pos["qty"] * (SPR / 2 * pos["credit"] + COMM / 2 * 100 / 100)
                turn[t] = 1
                pos = None
            gross[t] = pnl / eq
            ret[t] = (pnl - cost) / eq
            eq *= 1 + ret[t]
        elif pending:
            pending = False
            T_ = c["entry_dte"] / 365.0
            sg = vix[t]
            kps = _strike(S[t], T_, sg, r, c["short_delta"], True); kpl = _strike(S[t], T_, sg, r, 0.05, True)
            kcs = _strike(S[t], T_, sg, r, c["short_delta"], False); kcl = _strike(S[t], T_, sg, r, 0.05, False)
            p = dict(kps=kps, kpl=kpl, kcs=kcs, kcl=kcl, exp=idx[t] + pd.Timedelta(days=c["entry_dte"]))
            v0, _, _ = value(p, t)
            width = max(kps - kpl, kcl - kcs)
            if v0 >= 0.10 * width and v0 < width:     # catalog says 25%: unreachable with 5-delta wings in BS (14-20%), relaxed
                p["credit"] = v0; p["last"] = v0
                p["qty"] = 0.05 * eq / (width - v0)      # max loss = 5% of equity (qty in index-point units)
                cost = p["qty"] * (SPR / 2 * v0 + COMM / 2)
                ret[t] = -cost / eq; eq *= 1 + ret[t]
                pos = p; turn[t] = 1
        if pos is None and not pending:
            if (ivr[t] >= c["iv_rank_min"] and np.isfinite(rv20[t]) and (vix[t] - rv20[t]) > 0.03 and adx[t] < 25):
                pending = True
    return dict(gross=gross, net=ret, turn=turn)


# ----------------------------------------------------------------------------------------------- registry
SPY_ONLY = "SPY(VIX proxy)"
STRATS = {
    "connors_rsi2_ibs_reversion": dict(fn=connors, data="S&P500-today daily OHLC, VIX", grid={
        "rsi_period": [2, 3], "rsi_entry_threshold": [5, 10, 15], "ibs_threshold": [0.15, 0.2, 0.25],
        "exit_sma_period": [5, 7, 10], "max_positions": [5, 10]},
        caveats="survivorship-biased; entry/exit at close t signal -> open t+1 trade; earnings/news veto and BB confirm omitted; cash earns 0"),
    "idiosyncratic_residual_momentum": dict(fn=idio_resid_mom, data="S&P500-today daily", grid={
        "lookback_days": [126, 189, 252], "skip_recent_days": [10, 21], "top_n_holdings": [15, 20, 30],
        "sector_cap_pct": [0.25, 0.35]},
        caveats="survivorship-biased; residual vs SPY single-factor (rolling 252d beta), monthly rebalance; ATR trailing stop and SPY hedge leg omitted; sector cap = floor(cap*N) names; cash earns 0"),
    "kalman_cointegrated_pairs": dict(fn=kalman_pairs, data="18 fixed same-sector S&P pairs daily", grid={
        "lookback_window": [60, 90, 120], "entry_z": [1.75, 2.0, 2.25], "exit_z": [0.0, 0.25], "stop_z": [3.25, 3.5, 4.0]},
        caveats="survivorship-biased & pairs hand-picked (hindsight); cointegration p-value gate REPLACED by half-life 2-15d gate on the 1-state Kalman hedge-ratio spread; dollar-neutral, gross up to 2x; no borrow cost; earnings blackout omitted"),
    "turn_of_month_seasonality": dict(fn=turn_of_month, data="SPY/QQQ/IWM daily", grid={
        "entry_offset_days": [-2, -1], "exit_offset_days": [2, 3, 4], "target_etf": ["SPY", "QQQ", "IWM"],
        "bear_regime_scale": [0.0, 0.5, 1.0]},
        caveats="not survivorship-biased (ETFs); trades at next open (catalog: close); off-window cash earns 0 (no BIL yield credited); -4% stop omitted; exposure is ~25% of days so CAGR is not comparable with fully-invested sleeves"),
    "inverse_vol_trend_risk_parity": dict(fn=inv_vol_trend, data="SPY,EFA,TLT,GLD,DBC,BIL daily", grid={
        "vol_lookback_days": [20, 40, 60], "trend_sma_days": [150, 200, 210], "target_vol_annual": [0.08, 0.1, 0.12],
        "drift_band_pct": [0.02, 0.03]},
        caveats="ETFs, no survivorship issue; monthly decision, constant-weight daily rebalance assumed (no drift cost)"),
    "cross_sectional_low_vol_quality_box": dict(fn=lowvol_quality, data="S&P500-today daily", grid={
        "vol_window_days": [90, 126, 252], "top_n_stocks": [20, 25, 30], "rebalance_months": [1, 3]},
        caveats="survivorship-biased; QUALITY IS A PRICE PROXY (12m return > 0) because no fundamentals exist in repo; inverse-variance weights; Utilities+Staples capped 20%"),
    "pca_eigenportfolio_stat_arb": dict(fn=None, multi=pca_stat_arb_multi, data="100 liquid full-history S&P names", grid={
        "pca_window_days": [45, 60, 90], "num_eigenvectors": [3, 5, 8], "entry_s_score": [1.25, 1.5, 1.75],
        "max_half_life_days": [10, 15]},
        caveats="survivorship-biased (full-history survivors only); hedge = factor-projection residual portfolio, beta band not enforced; no borrow cost; 10 max positions, gross<=1"),
    "intraday_overnight_session_reversal": dict(fn=overnight_reversal, data="top-100 liquid S&P names daily open/close", grid={
        "entry_time_et": ["15:50"], "exit_time_et": ["09:31"], "decile_count": [5, 10, 15], "min_intraday_drop_pct": [-0.02, -0.03]},
        caveats="survivorship-biased; entry modelled at the CLOSE (not 15:55 limit) and exit at the OPEN (not 09:31-09:45); beta-adjusted laggards; time params collapse to one value (no intraday data); catalyst/FOMC filters omitted; 5 bps/side likely optimistic for MOC/open prints"),
    "week52_high_anchoring_momentum": dict(fn=week52, data="S&P500-today daily", grid={
        "high_lookback_days": [189, 252], "min_proximity": [0.9, 0.92, 0.95], "top_n_holdings": [15, 20, 25],
        "rebalance_days": [21, 42, 63]},
        caveats="survivorship-biased; hysteresis (top-40) and ATR trailing stop omitted; BIL throttle -> cash earns 0"),
    "intra_industry_5d_contrarian_reversal": dict(fn=intra_industry_reversal, data="S&P500-today daily, GICS sub-industry groups (>=4 names else sector)", grid={
        "excess_return_threshold": [-0.035, -0.045, -0.06], "hold_days": [3, 5, 7], "rsi_filter": [20, 25, 35]},
        caveats="survivorship-biased; news veto omitted; max 10 positions, 2 per group"),
    "bellwether_lead_lag_spillover": dict(fn=bellwether, data="S&P500-today daily; leader = highest 252d $volume name per sub-industry", grid={
        "leader_sigma_min": [2.0, 2.5, 3.0], "laggard_max_sigma": [0.5, 0.75], "hold_days": [3, 5, 7]},
        caveats="APPROXIMATION: leader mapping is automatic (not the catalog's hand-made clusters); sentiment/LLM catalyst filter and 60%-catch-up exit omitted; survivorship-biased"),
    "amihud_illiquidity_quality_factor": dict(fn=amihud_quality, data="S&P500-today daily", grid={
        "amihud_window_days": [40, 60, 90], "min_roe_pct": [0.1, 0.12, 0.15], "top_n_holdings": [15, 20, 25],
        "rebalance_days": [21, 42, 63]},
        caveats="survivorship-biased; ROE/FCF/leverage gate REPLACED by 12m-return percentile proxy (0.3/0.4/0.5 for 10/12/15%); ADV cap not modelled"),
    "vix_term_structure_roll_carry": dict(fn=vix_carry, data="^VIX, ^VIX3M, SVXY (ETF proxy) from 2011-10", grid={
        "min_contango_slope": [0.03, 0.05, 0.08], "max_vix_entry": [20, 22, 25], "exit_slope_threshold": [0.0, 0.01, 0.02]},
        caveats="PROXY: SVXY (short VIX futures ETF; leverage cut -1x -> -0.5x in Feb-2018) held at 100% when on, catalog caps sleeve at 15% NLV (scale linearly); bull-put-spread variant not modelled; XIV-style tail risk (Feb-2018) is in sample"),
    "iv_rank_iron_condor_harvest": dict(fn=iron_condor, data="SPY + VIX (modelled BS premiums)", grid={
        "entry_dte": [30, 38, 45], "short_delta": [0.12, 0.16, 0.2], "iv_rank_min": [40, 50, 65],
        "take_profit_pct": [0.4, 0.5, 0.6], "exit_dte": [7, 14, 21]},
        caveats="MODELLED PREMIUMS, NOT REAL FILLS: flat-vol Black-Scholes at VIX (no skew/term structure), 3% round-trip spread + commissions, SPY only; catalog min credit 25% of width gives ZERO trades under this model (credit is 14-20% with 5-delta wings) so 10% used; FOMC/CPI guard and vega cap omitted; fixed 5% max-loss sizing"),
}
NOT_TESTABLE = {
    "pead_earnings_surprise_drift": "No earnings dates/EPS surprises (SUE) anywhere in repo data; would need historical EPS actual/estimate + report dates.",
    "pre_earnings_iv_runup": "No earnings calendar and no historical option chains/IV term structure; would need per-name earnings dates + option IV history.",
    "options_put_skew_credit_spread": "No skew data (no CBOE SKEW/25-delta risk reversals/chains); flat-vol BS pricer carries zero skew so the signal cannot be built; would need historical chains or SKEW index + put IV by strike.",
    "forward_variance_calendar_arb": "Needs front/back option IVs by expiry (forward variance from chains); only VIX/VIX3M/VIX9D index levels exist, and calendar spread P&L cannot be priced from them credibly.",
    "triple_barrier_meta_labeling": "DEFERRED: needs an ML meta-labeller wrapped around the live swing signal generator (walk-forward training); not feasible quickly without touching app/ code.",
    "intraday_vwap_unconfirmed_gap_fade": "Intraday data (data/bars_intraday M5/M15, 87 symbols) spans 2026-05-29..2026-11-24, entirely AFTER the 2025-11-21 locked holdout; cannot be used.",
    "orb_rvol_narrow_range": "Same: all intraday bars are post-holdout (2026-05..2026-11, ~120 sessions).",
    "volume_clock_order_flow_imbalance": "Same: post-holdout intraday only, and no trade/tick data for true order-flow imbalance (bar proxy only).",
}
