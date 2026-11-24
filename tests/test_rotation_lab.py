import numpy as np
import pandas as pd

from app.rotation_lab.engine import (Spec, features, regime_series, rebal_flags, build_targets, run,
                                     SLIP, SEC_FEE)


def _panel(T=400, M=6, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=T)
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, (T, M)), axis=0)), index=idx,
                     columns=[f"S{i}" for i in range(M)])
    return c


def _targets(c, spec=None):
    spec = spec or Spec(score="mom121", topn=2, rebal="daily")
    f = features(c)
    sc = f["mom121"]
    return build_targets(sc, ~np.isnan(sc), f["vol"], np.ones(len(c), bool), rebal_flags(c.index, "daily"),
                         spec, f["absok"])


def test_no_lookahead():
    c = _panel()
    W1, _ = _targets(c)
    c2 = c.copy(); c2.iloc[300:] *= np.random.default_rng(1).uniform(0.5, 2, c2.iloc[300:].shape)
    W2, _ = _targets(c2)
    assert np.allclose(W1[:300], W2[:300])


def test_next_open_execution():
    T = 5
    O = np.array([[10., 1], [10., 1], [20., 1], [20., 1], [20., 1]])
    C = O.copy()
    W = np.zeros((T, 2)); W[:, 0] = 1.0
    flag = np.zeros(T, bool); flag[1] = True      # decided at close 1 -> trade at open 2 (price 20)
    r, turn = run(O, C, W, flag, costs=False)
    assert turn[2] == 2.0 and turn[1] == 0.0
    assert abs(r[1]) < 1e-12            # still in cash through open1->open2; no gain from the 10->20 jump
    assert abs(r[2]) < 1e-12


def test_costs_on_turnover():
    T = 4
    O = np.ones((T, 2)) * 50.0; C = O.copy()
    W = np.zeros((T, 2)); W[1:, 0] = 1.0; W[0, 1] = 1.0; W[:, 1] = 1 - W[:, 0]
    flag = np.array([False, True, False, False])
    r, turn = run(O, C, W, flag, costs=True)
    assert turn[2] == 2.0 or abs(turn[2] - 2.0) < 1e-9
    expected = SLIP * 2.0 + SEC_FEE * 1.0 + (0.000166 / 50.0)
    assert abs(-r[2] - expected) < 1e-9


def test_regime_switch_goes_to_riskoff():
    c = _panel()
    f = features(c); sc = f["mom121"]
    reg = np.ones(len(c), bool); reg[350:] = False
    spec = Spec(score="mom121", topn=2, rebal="weekly")
    W, flag = build_targets(sc, ~np.isnan(sc), f["vol"], reg, rebal_flags(c.index, "weekly"), spec, f["absok"])
    assert W[349, :-1].sum() > 0.99 and W[350, -1] == 1.0 and flag[350]


def test_mom_12_1_definition():
    c = _panel(T=300)
    m = features(c)["mom121"]
    t = 280
    exp = c.iloc[t - 21].values / c.iloc[t - 252].values - 1
    assert np.allclose(m[t], exp)
    assert np.isnan(m[250]).all()


def test_missing_history_excluded():
    c = _panel()
    c.iloc[:350, 0] = np.nan                       # S0 has no history before row 350
    W, _ = _targets(c, Spec(score="mom121", topn=6, rebal="daily"))
    assert (W[:350 + 252, 0] == 0).all()


# ---------------------------------------------------------------- catalog tests
from app.rotation_lab import catalog as cat


def _etf_panel(T=700, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=T)
    cols = ["SPY", "EFA", "AGG", "IEF", "TLT", "BIL"]
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.008, (T, len(cols))), axis=0)), index=idx, columns=cols)
    return cat.Panel(c.copy(), c)


def test_catalog_monthly_sma_no_lookahead():
    P = _etf_panel()
    W1, f1 = cat.faber(P, assets=["SPY", "EFA", "IEF"], sma=10)
    c2 = P.c.copy(); c2.iloc[500:] *= 3.0
    P2 = cat.Panel(c2.copy(), c2)
    W2, f2 = cat.faber(P2, assets=["SPY", "EFA", "IEF"], sma=10)
    assert np.allclose(W1.values[:500], W2.values[:500]) and (f1[:500] == f2[:500]).all()
    assert f1.sum() > 10 and W1.values.sum() > 0


def test_catalog_sma_rule_exact():
    P = _etf_panel()
    W, flag = cat.faber(P, assets=["SPY"], sma=10)
    t = np.where(flag)[0][-1]
    ix, M = cat.monthend_table(P, ["SPY"]); i = list(ix).index(t)
    assert (W["SPY"].values[t] == 1.0) == bool(M.iloc[i, 0] > M.iloc[i - 9:i + 1, 0].mean())
    assert t == ix[-1]            # signals only on month-end closes


def test_catalog_calendar_dates():
    P = _etf_panel()
    f, l = cat._month_pos(P.idx)
    m = (l <= 1) | (f <= 3)
    ds = P.idx[m]
    for d in ds[:12]:  # every held day is the last trading day or one of the first 3 of a month
        assert d == P.idx[P.idx.to_period("M") == d.to_period("M")][-1] or d in P.idx[P.idx.to_period("M") == d.to_period("M")][:3]
    W, flag = cat.tom(P)
    w = W["SPY"].values; t = np.where((w[1:] == 1) & (w[:-1] == 0))[0][0] + 1       # decided at close t -> first held day is t+1
    assert m[t + 1] and f[t + 1] == 1 or l[t + 1] <= 1
    hw = cat.halloween(P)[0]["SPY"].values
    assert hw[np.where(P.idx.month.values[1:] == 12)[0][0]] == 1.0 and hw[np.where(P.idx.month.values[1:] == 7)[0][0]] == 0.0


def test_catalog_dual_momentum_switching():
    idx = pd.bdate_range("2019-01-01", periods=600)
    n = len(idx)
    spy = pd.Series(100 * 1.0006 ** np.arange(n), index=idx)
    efa = pd.Series(100 * 1.0003 ** np.arange(n), index=idx)
    agg = pd.Series(100 * 1.0001 ** np.arange(n), index=idx); bil = pd.Series(100.0, index=idx)
    spy.iloc[450:] = spy.iloc[450] * 0.999 ** np.arange(n - 450)            # US crashes: abs momentum < cash
    c = pd.DataFrame({"SPY": spy, "EFA": efa, "AGG": agg, "BIL": bil})
    P = cat.Panel(c.copy(), c)
    W, flag = cat.gem(P)
    early = W.iloc[np.where(flag)[0][3]]; late = W.iloc[-1]
    assert early["SPY"] == 1.0 and early.sum() == 1.0
    assert late["AGG"] == 1.0 and late["SPY"] == 0.0


def test_catalog_risk_parity_weights_sum_to_one():
    P = _etf_panel()
    W, flag = cat.risk_parity(P, assets=("SPY", "EFA", "TLT"), lb=126)
    s = W.values.sum(axis=1)[flag]
    assert len(s) > 5 and np.allclose(s, 1.0)
    assert (W.values[flag] > 0).all()


def test_catalog_engine_cash_remainder_and_no_leverage():
    P = _etf_panel()
    W, flag = cat.vaa(P, ["SPY", "EFA", "AGG"], ["IEF", "BIL"], B=1, topo=1)
    assert (W.values.sum(axis=1) <= 1.0 + 1e-9).all()
    r, t = cat.evaluate(P, W, flag)
    assert np.isfinite(r.values).all()
