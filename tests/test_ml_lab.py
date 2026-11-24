import numpy as np
import pandas as pd
from app.ml_lab import core, exp


def _panel(T=700, M=6, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=T)
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, .01, (T, M)), 0)), index=idx, columns=list("ABCDEF")[:M])
    v = pd.DataFrame(rng.integers(1e5, 1e6, (T, M)).astype(float), index=idx, columns=c.columns)
    spy = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, .008, T))), index=idx)
    return c, v, spy


def test_features_no_lookahead():
    c, v, spy = _panel()
    sec = {s: "X" for s in c.columns}
    f1 = core.compute_features(c, v, spy, sec)
    c2, v2, s2 = c.copy(), v.copy(), spy.copy()
    c2.iloc[500:] *= 3; v2.iloc[500:] *= 5; s2.iloc[500:] *= 2      # tamper with the future
    f2 = core.compute_features(c2, v2, s2, sec)
    for k in core.FEATURES:
        pd.testing.assert_frame_equal(f1[k].iloc[:500], f2[k].iloc[:500], check_exact=False)


def test_walk_forward_embargo_and_expanding():
    idx = pd.bdate_range("2010-01-01", "2020-12-31")
    dec = core.month_decisions(idx)
    ends = core.label_end_pos(dec)
    prev = -1
    for y, tr, te in core.walk_forward_splits(dec, idx, 2013, 2020, embargo=21):
        first = dec[te[0]] + 1
        assert (ends[tr] + 21 <= first).all()
        assert (tr < te.min()).all() and len(tr) >= prev
        assert set(idx.year[dec][te]) == {y}
        prev = len(tr)


def test_embargo_ge_horizon_weekly4():
    idx = pd.bdate_range("2010-01-01", "2020-12-31")
    dec = core.week_decisions(idx)
    ends = core.label_end_pos(dec, 4)
    for y, tr, te in core.walk_forward_splits(dec, idx, 2014, 2020, embargo=21, step=4):
        assert (ends[tr] + 21 <= dec[te[0]] + 1).all()


def test_labels_align_t_to_next_month():
    idx = pd.bdate_range("2020-01-01", periods=200)
    o = pd.DataFrame({"A": np.arange(200, dtype=float) + 100}, index=idx)
    dec = core.month_decisions(idx)
    lab = core.forward_labels(o, dec)
    i = 2
    assert np.isclose(lab[i, 0], o["A"].iloc[dec[i + 1] + 1] / o["A"].iloc[dec[i] + 1] - 1)
    assert np.isnan(lab[-1, 0])
    assert idx[dec[i]].month != idx[dec[i] + 1].month      # decision = last trading day of month


def test_portfolio_trades_next_open():
    T = 10
    idx = pd.bdate_range("2020-01-01", periods=T)
    class P: pass
    p = P(); p.index = idx
    o = np.array([100, 100, 100, 100, 100, 100, 100, 100, 100, 100.0])
    ocol = o.copy(); ocol[3:] = 110.0                       # price jump at open of day 3
    p.open = pd.DataFrame({"A": ocol, "BIL": 100.0}, index=idx)
    p.close = p.open.copy()
    W = np.zeros((T, 2)); W[:, 1] = 1; fl = np.zeros(T, bool)
    W[2:, 0] = 1; W[2:, 1] = 0; fl[2] = True               # decided at close of day 2
    r, t = exp.simulate(p, ["A"], "BIL", W, fl)
    assert abs(r.iloc[2]) < 1e-9 and t.iloc[2] == 0           # decision day: nothing traded yet
    assert t.iloc[3] == 2.0                                   # traded (BIL -> A) at open of day 3
    assert r.iloc[3] < 0 and abs(r.iloc[3]) < 0.01            # only costs; the gap into open[3] not captured


def test_registry_append_only(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "REGISTRY", tmp_path / "reg.csv")
    m = dict(cagr=.1, sharpe=1., maxdd=-.1, turnover=2.)
    core.log_trial("a", "c1", "u", "2013-01-01", "2020-01-01", m)
    core.log_trial("b", "c2", "u", "2013-01-01", "2020-01-01", m)
    lines = (tmp_path / "reg.csv").read_text().splitlines()
    assert lines[0].split(",") == core.HEADER and len(lines) == 3
    assert core.n_trials() == 304 + 2
