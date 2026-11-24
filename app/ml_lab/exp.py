"""Experiments A (cross-sectional ranking) and B (SPY timing classifier). Research only."""
from __future__ import annotations
import math, time
import numpy as np
import pandas as pd
from app.rotation_lab.engine import run, apply_voltarget       # read-only import (portfolio backtester)
from . import core
from .core import (Panel, compute_features, month_decisions, week_decisions, forward_labels, walk_forward_splits,
                   xs_rank, FEATURES, metrics, log_trial)
from .models import Ridge, LGB

VT_CAP = 0.15


# ---------------- portfolio helpers ----------------
def build_weights(scores: np.ndarray, dec: np.ndarray, T: int, topn: int):
    """scores (n_dec, M) NaN=ineligible. Decision at close dec[i]; held until next decision. Last col = risk-off."""
    n, M = scores.shape
    W = np.zeros((T, M + 1)); W[:, M] = 1.0
    flag = np.zeros(T, bool)
    for i in range(n):
        s = scores[i]
        fin = np.isfinite(s)
        if not fin.any():
            continue
        k = min(topn, int(fin.sum()))
        ix = np.argpartition(-np.where(fin, s, -np.inf), k - 1)[:k]
        w = np.zeros(M + 1); w[ix] = 1.0 / topn; w[M] = 1 - w[:M].sum()
        end = dec[i + 1] if i + 1 < n else T
        W[dec[i]:end] = w
        flag[dec[i]] = True
    return W, flag


def simulate(panel: Panel, cols: list, riskoff: str, W: np.ndarray, flag: np.ndarray, voltarget: float = 0.0):
    O = np.column_stack([panel.open[cols].values, panel.open[riskoff].values])
    C = np.column_stack([panel.close[cols].values, panel.close[riskoff].values])
    if voltarget > 0:
        r0, _ = run(O, C, W, flag, costs=False)
        W, flag = apply_voltarget(W, flag, r0, voltarget)
    r, t = run(O, C, W, flag)
    return pd.Series(r, index=panel.index), pd.Series(t, index=panel.index)


def yearly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.year).prod() - 1


# ---------------- Experiment A ----------------
def spearman_rows(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    out = np.full(len(p), np.nan)
    for i in range(len(p)):
        m = np.isfinite(p[i]) & np.isfinite(y[i])
        if m.sum() > 30:
            a = pd.Series(p[i][m]).rank().values; b = pd.Series(y[i][m]).rank().values
            out[i] = np.corrcoef(a, b)[0, 1]
    return out


def build_dataset(panel: Panel):
    cols = panel.stocks
    f = compute_features(panel.close[cols], panel.volume[cols], panel.close["SPY"], panel.sector)
    dec = month_decisions(panel.index)
    raw = np.stack([f[k].values[dec] for k in FEATURES], axis=2)          # (n, M, F)
    valid = np.isfinite(raw).all(axis=2)
    X = np.stack([xs_rank(np.where(valid, raw[:, :, j], np.nan)) for j in range(raw.shape[2])], axis=2)
    lab = forward_labels(panel.open[cols], dec)
    lab = np.where(valid, lab, np.nan)
    y = xs_rank(lab)
    return cols, dec, X, valid, lab, y, f["mom121"].values[dec]


def run_a(panel: Panel, first_test_year: int = 2013, last_year: int = 2025, log: bool = True):
    t0 = time.time()
    cols, dec, X, valid, lab, y, mom = build_dataset(panel)
    n, M, F = X.shape
    preds = {k: np.full((n, M), np.nan) for k in ("ridge", "lgbm")}
    splits = list(walk_forward_splits(dec, panel.index, first_test_year, last_year, embargo=21, step=1))
    for yr, tr, te in splits:
        m = np.isfinite(y[tr]) & valid[tr]
        Xtr = X[tr][m]; ytr = y[tr][m]
        for name, mdl in (("ridge", Ridge()), ("lgbm", LGB())):
            mdl.fit(Xtr, ytr)
            for i in te:
                v = valid[i]
                if v.any():
                    preds[name][i, v] = mdl.predict(X[i][v])
    preds["ens"] = (xs_rank(preds["ridge"]) + xs_rank(preds["lgbm"])) / 2
    base = np.where(np.isfinite(preds["ridge"]), mom, np.nan)               # plain 12-1 on the same dates/universe
    preds["mom121"] = base
    test_rows = np.concatenate([te for _, _, te in splits])
    start_pos = dec[test_rows[0]] + 1
    start = panel.index[start_pos]
    end = panel.index[-1]

    # IC
    ic = {k: spearman_rows(p, lab) for k, p in preds.items()}
    dates = panel.index[dec]
    ic_df = pd.DataFrame(ic, index=dates).iloc[test_rows]
    ic_df = ic_df.dropna(how="all")
    ic_year = ic_df.groupby(ic_df.index.year).mean()
    hit_year = (ic_df > 0).groupby(ic_df.index.year).mean()
    ic_t = ic_df.mean() / ic_df.std() * math.sqrt(len(ic_df))

    # portfolios
    T = len(panel.index)
    res = {}
    rets = {}
    for model in ("ridge", "lgbm", "ens", "mom121"):
        for N in (10, 20):
            W, fl = build_weights(preds[model], dec, T, N)
            for vt in (0.0, VT_CAP):
                r, tn = simulate(panel, cols, "BIL", W, fl, vt)
                key = f"{model}|N{N}|{'vt' if vt else 'ew'}"
                m_ = metrics(r, tn, start)
                res[key] = m_; rets[key] = r[r.index >= start]
                if log:
                    log_trial(f"A_{key}", f"A|{key}|{first_test_year}", "sp500_today", start, end, m_,
                              "survivorship-biased univ; WF yearly; test from %d" % first_test_year)
    spy_r = panel.close["SPY"].pct_change().fillna(0)
    spy_bh = (panel.open["SPY"].shift(-1) / panel.open["SPY"] - 1).fillna(0)
    spy_bh = spy_bh[spy_bh.index >= start]
    res["SPY_BH"] = metrics(spy_bh, None); rets["SPY_BH"] = spy_bh
    # baseline over the full 2011 window for context
    W, fl = build_weights(np.where(valid, mom, np.nan), dec, T, 10)
    r, tn = simulate(panel, cols, "BIL", W, fl, 0.0)
    full_base = metrics(r, tn, core.EVAL_START)
    # save predictions
    rows = []
    for k in ("ridge", "lgbm", "ens"):
        p = preds[k][test_rows]
        d = pd.DataFrame(p, index=dates[test_rows], columns=cols).stack().rename("pred").reset_index()
        d.columns = ["date", "symbol", "pred"]; d["model"] = k
        rows.append(d)
    core.OUT.mkdir(parents=True, exist_ok=True)
    pd.concat(rows).to_csv(core.OUT / "predictions_A.csv.gz", index=False, float_format="%.5f")
    return dict(res=res, rets=rets, ic_year=ic_year, hit_year=hit_year, ic_t=ic_t, ic_mean=ic_df.mean(),
                start=start, end=end, full_base=full_base, runtime=time.time() - t0, n_train_last=len(splits[-1][1]))


# ---------------- Experiment B ----------------
def timing_features(panel: Panel):
    c = panel.close
    spy = c["SPY"]; r1 = spy.pct_change()
    sma = c[panel.stocks].rolling(200, min_periods=200).mean()
    ok = sma.notna()
    breadth = ((c[panel.stocks] > sma) & ok).sum(axis=1) / ok.sum(axis=1).replace(0, np.nan)
    f = pd.DataFrame(index=c.index)
    f["spy4w"] = spy / spy.shift(20) - 1
    f["spy13w"] = spy / spy.shift(65) - 1
    f["spy26w"] = spy / spy.shift(130) - 1
    f["spy_sma200"] = spy / spy.rolling(200, min_periods=200).mean() - 1
    f["vol20"] = r1.rolling(20).std() * math.sqrt(252)
    f["volratio"] = r1.rolling(20).std() / r1.rolling(120).std()
    f["dd252"] = spy / spy.rolling(252, min_periods=252).max() - 1
    f["breadth"] = breadth
    f["breadth_chg"] = breadth - breadth.shift(20)
    f["curve"] = (c["IEF"] / c["IEF"].shift(65) - 1) - (c["BIL"] / c["BIL"].shift(65) - 1)    # yield-curve proxy
    f["term"] = (c["TLT"] / c["TLT"].shift(65) - 1) - (c["IEF"] / c["IEF"].shift(65) - 1)
    return f


def run_b(panel: Panel, first_test_year: int = 2014, last_year: int = 2025, log: bool = True):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score
    import lightgbm as lgb
    t0 = time.time()
    f = timing_features(panel)
    dec = week_decisions(panel.index)
    Xall = f.values[dec]
    valid = np.isfinite(Xall).all(axis=1)
    O = panel.open
    step = 4
    ls = forward_labels(O[["SPY"]], dec, step)[:, 0]; lb = forward_labels(O[["BIL"]], dec, step)[:, 0]
    exc = ls - lb
    yb = np.where(np.isfinite(exc), (exc > 0).astype(float), np.nan)
    splits = list(walk_forward_splits(dec, panel.index, first_test_year, last_year, embargo=21, step=step))
    P = {"logit": np.full(len(dec), np.nan), "lgbm": np.full(len(dec), np.nan)}
    for yr, tr, te in splits:
        m = valid[tr] & np.isfinite(yb[tr])
        Xt, yt = Xall[tr][m], yb[tr][m]
        sc = StandardScaler().fit(Xt)
        lg = LogisticRegression(C=0.1, max_iter=500).fit(sc.transform(Xt), yt)
        gb = lgb.LGBMClassifier(n_estimators=100, learning_rate=0.03, num_leaves=4, min_child_samples=40, subsample=0.7,
                                subsample_freq=1, colsample_bytree=0.7, reg_lambda=10, random_state=0, verbose=-1, n_jobs=4).fit(Xt, yt)
        for i in te:
            if valid[i]:
                P["logit"][i] = lg.predict_proba(sc.transform(Xall[i:i + 1]))[0, 1]
                P["lgbm"][i] = gb.predict_proba(Xall[i:i + 1])[0, 1]
    test_rows = np.concatenate([te for _, _, te in splits])
    start = panel.index[dec[test_rows[0]] + 1]; end = panel.index[-1]
    T = len(panel.index)
    sma_sig = (f["spy_sma200"].values[dec] > 0).astype(float)
    sma_sig[~valid] = np.nan
    auc = {}
    for k in P:
        m = np.isfinite(P[k]) & np.isfinite(yb)
        yrs = np.asarray(panel.index.year)[dec]
        auc[k] = {int(y_): (roc_auc_score(yb[m & (yrs == y_)], P[k][m & (yrs == y_)])
                            if len(set(yb[m & (yrs == y_)])) > 1 else float("nan")) for y_ in sorted(set(yrs[m]))}
        auc[k]["ALL"] = roc_auc_score(yb[m], P[k][m])
    res, rets = {}, {}
    sig = {"logit": (P["logit"] > 0.5).astype(float), "lgbm": (P["lgbm"] > 0.5).astype(float), "sma200": sma_sig}
    for k in ("logit", "lgbm"):
        sig[k][~np.isfinite(P[k])] = np.nan
    for ro in ("IEF", "BIL"):
        for k, s in sig.items():
            W = np.zeros((T, 2)); W[:, 1] = 1.0; fl = np.zeros(T, bool)
            for i in range(len(dec)):
                if np.isfinite(s[i]):
                    end_ = dec[i + 1] if i + 1 < len(dec) else T
                    W[dec[i]:end_, 0] = s[i]; W[dec[i]:end_, 1] = 1 - s[i]; fl[dec[i]] = True
            r, tn = simulate(panel, ["SPY"], ro, W, fl)
            key = f"{k}|{ro}"
            m_ = metrics(r, tn, start); res[key] = m_; rets[key] = r[r.index >= start]
            if log:
                log_trial(f"B_{key}", f"B|{key}|{first_test_year}", "SPY", start, end, m_, "weekly; label SPY>BIL 4w")
    spy_bh = (O["SPY"].shift(-1) / O["SPY"] - 1).fillna(0)
    spy_bh = spy_bh[spy_bh.index >= start]
    res["SPY_BH"] = metrics(spy_bh, None); rets["SPY_BH"] = spy_bh
    return dict(res=res, rets=rets, auc=auc, start=start, end=end, runtime=time.time() - t0,
                base_rate=float(np.nanmean(yb[test_rows])))


def dsr_table(rets: dict, res: dict) -> dict:
    nt = core.n_trials()
    sr_var = max(core.ROT_SR_VAR, float(np.nanvar([v["daily_sr"] for v in res.values() if np.isfinite(v["daily_sr"])])))
    return {k: core.deflated_sharpe(r, nt, sr_var) for k, r in rets.items()}, nt, sr_var
