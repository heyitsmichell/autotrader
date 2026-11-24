"""Research-only: candle-feature ML for swing entries. Not wired into live trading."""
from __future__ import annotations
import glob, os
import numpy as np
import pandas as pd

LAGS = 10
SLIP = 0.0005  # per side
HOLD = 10


def load_bars(d="data/bars_yahoo"):
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*.csv"))):
        df = pd.read_csv(f, parse_dates=["date"]).dropna().set_index("date").sort_index()
        out[os.path.basename(f)[:-4]] = df
    return out


def _atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def build_features(df: pd.DataFrame, spy: pd.DataFrame | None = None) -> pd.DataFrame:
    """Row t uses only bars <= t (all ops are causal: shift/rolling/ewm)."""
    o, h, l, c, v = (df[k] for k in ("open", "high", "low", "close", "volume"))
    atr = _atr(df)
    f = {}
    body = (c - o) / atr
    up = (h - np.maximum(o, c)) / atr
    lo = (np.minimum(o, c) - l) / atr
    ret = c.diff() / atr
    gap = (o - c.shift(1)) / atr
    for k in range(LAGS):
        f[f"body{k}"], f[f"uw{k}"], f[f"lw{k}"] = body.shift(k), up.shift(k), lo.shift(k)
        f[f"ret{k}"], f[f"gap{k}"] = ret.shift(k), gap.shift(k)
    f["vol_rel"] = v / v.rolling(20).mean()
    s20, s50 = c.rolling(20).mean(), c.rolling(50).mean()
    f["d_sma20"], f["d_sma50"] = (c - s20) / atr, (c - s50) / atr
    h20, l20 = h.rolling(20).max(), l.rolling(20).min()
    f["pos20"] = (c - l20) / (h20 - l20).replace(0, np.nan)
    f["rsi"] = _rsi(c) / 100
    ap = atr / c
    f["atr_pct"] = ap
    f["atr_rank"] = ap.rolling(120, min_periods=60).apply(lambda x: (x <= x[-1]).mean(), raw=True)
    r20 = c.pct_change(20)
    if spy is not None:
        s = spy.close.reindex(df.index).ffill()
        f["rs20"] = r20 - s.pct_change(20)
        f["spy_trend"] = (s / s.rolling(50).mean() - 1)
    else:
        f["rs20"] = r20
        f["spy_trend"] = 0.0
    rng = (h - l).replace(0, np.nan)
    f["inside"] = ((h < h.shift(1)) & (l > l.shift(1))).astype(float)
    f["engulf"] = ((c > o) & (c.shift(1) < o.shift(1)) & (c >= o.shift(1)) & (o <= c.shift(1))).astype(float)
    f["hammer"] = (((np.minimum(o, c) - l) > 2 * (c - o).abs()) & ((h - np.maximum(o, c)) < 0.3 * rng)).astype(float)
    f["breakout"] = (c > h.rolling(20).max().shift(1)).astype(float)
    f["pullback"] = (((c - s20).abs() < 0.5 * atr) & (s20 > s50) & (c > s50)).astype(float)
    out = pd.DataFrame(f, index=df.index)
    out["atr"] = atr
    return out


def label_trade(o, h, l, c, i, atr, tgt_mult=1.5, hold=HOLD):
    """Signal at close of bar i; enter at open of i+1. Returns (win, R_gross, R_net, exit_idx) or None.
    stop = 2*ATR clamped to 1%-8% of entry; target = tgt_mult*ATR. Stop wins ties within a bar."""
    j = i + 1
    if j + hold - 1 >= len(o) or not np.isfinite(atr) or atr <= 0:
        return None
    e = o[j]
    risk = min(max(2 * atr, 0.01 * e), 0.08 * e)
    stop, tgt = e - risk, e + tgt_mult * atr
    win, exit_px, xi = 0, None, j + hold - 1
    for k in range(j, j + hold):
        if l[k] <= stop:
            exit_px, xi = stop, k
            break
        if h[k] >= tgt:
            exit_px, xi, win = tgt, k, 1
            break
    if exit_px is None:
        exit_px = c[xi]
    gross = (exit_px - e) / risk
    net = (exit_px * (1 - SLIP) - e * (1 + SLIP)) / risk
    return win, gross, net, xi


def build_dataset(bars, tgt_mult=1.5):
    spy = bars.get("SPY")
    rows = []
    for sym, df in bars.items():
        if sym == "SPY" or len(df) < 150:
            continue
        F = build_features(df, spy)
        o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
        atr = F["atr"].values
        lab = np.full((len(df), 4), np.nan)
        for i in range(len(df) - 1):
            r = label_trade(o, h, l, c, i, atr[i], tgt_mult)
            if r:
                lab[i] = r
        F["win"], F["r_gross"], F["r_net"], F["exit_idx"] = lab.T
        F["sym"], F["di"] = sym, np.arange(len(df))
        F["date"] = df.index
        rows.append(F)
    D = pd.concat(rows).reset_index(drop=True)
    feat = [x for x in D.columns if x not in ("atr", "win", "r_gross", "r_net", "exit_idx", "sym", "di", "date")]
    D = D.dropna(subset=feat + ["win"]).reset_index(drop=True)
    dates = {s: bars[s].index for s in bars}
    D["exit_date"] = [dates[s][int(x)] for s, x in zip(D.sym, D.exit_idx)]
    D["entry_date"] = [dates[s][int(i) + 1] for s, i in zip(D.sym, D.di)]
    return D, feat


def walk_forward_folds(dates, n_folds=5, embargo=HOLD, min_train_frac=1 / 6):
    """dates: dates array. Expanding train; train ends `embargo+1` days before test start
    (label of day t spans t+1..t+HOLD so the +1 guarantees no label overlap)."""
    u = np.asarray(sorted(set(dates)))
    n = len(u)
    start = int(n * min_train_frac)
    edges = np.linspace(start, n, n_folds + 1).astype(int)
    folds = []
    for a, b in zip(edges[:-1], edges[1:]):
        tr_end = a - embargo - 1
        folds.append((u[0], u[tr_end], u[a], u[b - 1]))  # train_start, train_end, test_start, test_end
    return folds


def auc(y, s):
    y = np.asarray(y)
    s = np.asarray(s)
    n1 = y.sum()
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    r = pd.Series(s).rank().values
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def fit_predict(kind, Xtr, ytr, Xte, seed=0):
    import torch
    torch.manual_seed(seed)
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    A = torch.tensor(np.clip((Xtr - mu) / sd, -5, 5), dtype=torch.float32)
    B = torch.tensor(np.clip((Xte - mu) / sd, -5, 5), dtype=torch.float32)
    y = torch.tensor(ytr, dtype=torch.float32)
    d = A.shape[1]
    if kind == "logreg":
        m = torch.nn.Linear(d, 1)
        wd, ep = 1e-3, 150
    else:
        m = torch.nn.Sequential(torch.nn.Linear(d, 32), torch.nn.ReLU(), torch.nn.Dropout(0.3), torch.nn.Linear(32, 1))
        wd, ep = 1e-2, 25
    opt = torch.optim.AdamW(m.parameters(), lr=1e-2 if kind == "logreg" else 2e-3, weight_decay=wd)
    lf = torch.nn.BCEWithLogitsLoss()
    for _ in range(ep):
        m.train()
        perm = torch.randperm(len(A)) if kind != "logreg" else torch.arange(len(A))
        bs = 2048 if kind != "logreg" else len(A)
        for k in range(0, len(A), bs):
            idx = perm[k:k + bs]
            opt.zero_grad()
            lf(m(A[idx]).squeeze(1), y[idx]).backward()
            opt.step()
    m.eval()
    with torch.no_grad():
        return m(B).squeeze(1).numpy()


def simulate(picks: pd.DataFrame, max_pos=5, risk=0.01):
    """Greedy by entry date, best score first, max concurrent positions; equity compounds
    `risk` per R at exit. Returns (trades_taken, final_equity, maxDD)."""
    taken, open_exits = [], []
    for d, g in picks.sort_values(["entry_date", "score"], ascending=[True, False]).groupby("entry_date", sort=True):
        open_exits = [x for x in open_exits if x >= d]
        for _, r in g.iterrows():
            if len(open_exits) >= max_pos:
                break
            open_exits.append(r.exit_date)
            taken.append(r)
    if not taken:
        return pd.DataFrame(), 1.0, 0.0
    T = pd.DataFrame(taken).sort_values("exit_date")
    eq = np.cumprod(1 + risk * T.r_net.values)
    peak = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    return T, float(eq[-1]), float(((eq / peak) - 1).min())


def evaluate(D, feat, n_folds=5, models=("logreg", "mlp"), top_q=0.9, seed=0):
    folds = walk_forward_folds(D.date.values, n_folds)
    res = []
    rng = np.random.default_rng(seed)
    for fi, (t0, t1, s0, s1) in enumerate(folds):
        tr = D[(D.date >= t0) & (D.date <= t1)]
        te = D[(D.date >= s0) & (D.date <= s1)].copy()
        base = te.win.mean()
        sets = {}
        for m in models:
            te["score"] = fit_predict(m, tr[feat].values, tr.win.values, te[feat].values, seed)
            sets[m] = (te.copy(), te.score.quantile(top_q), None)
        rmask = (te.breakout == 1) & (te.vol_rel > 1.5)
        rule = te.copy()
        rule["score"] = rule.vol_rel
        sets["rule_breakout_vol"] = (rule[rmask], -np.inf, rmask.astype(float).values)
        for name, (S, thr, rscore) in sets.items():
            a = auc(S.win.values, S.score.values) if rscore is None else auc(te.win.values, rscore)
            P = S[S.score >= thr]
            T, eq, dd = simulate(P)
            rexp = []
            for _ in range(5):
                R = te.sample(min(len(T), len(te)), random_state=int(rng.integers(1e9))).copy()
                rexp.append(R.r_net.mean())
            res.append(dict(fold=fi, test=f"{str(s0)[:10]}..{str(s1)[:10]}", model=name, auc=a,
                            n_picks=len(P), trades=len(T), hit=float(T.win.mean()) if len(T) else np.nan,
                            expR_pick=float(P.r_net.mean()) if len(P) else np.nan,
                            expR_taken=float(T.r_net.mean()) if len(T) else np.nan,
                            ret=eq - 1, maxdd=dd, rand_expR=float(np.mean(rexp)),
                            base_hit=base, all_expR=float(te.r_net.mean())))
    return pd.DataFrame(res)
