import numpy as np, pandas as pd
from app.ml_candles import build_features, label_trade, walk_forward_folds, HOLD


def _df(n=200, seed=1):
    r = np.random.default_rng(seed)
    c = 100 + np.cumsum(r.normal(0, 1, n))
    o = c + r.normal(0, .3, n)
    h = np.maximum(o, c) + abs(r.normal(0, .5, n))
    l = np.minimum(o, c) - abs(r.normal(0, .5, n))
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=r.uniform(1e6, 2e6, n)),
                        index=pd.bdate_range("2022-01-03", periods=n))


def test_features_no_lookahead():
    d = _df()
    d2 = d.copy()
    d2.iloc[150:] = d2.iloc[150:] * 1.7 + 5
    pd.testing.assert_frame_equal(build_features(d).iloc[:150], build_features(d2).iloc[:150])


def test_features_no_lookahead_with_spy():
    d, s = _df(), _df(seed=2)
    s2 = s.copy()
    s2.iloc[150:] *= 2
    pd.testing.assert_frame_equal(build_features(d, s).iloc[:150], build_features(d, s2).iloc[:150])


def _arr(rows):
    a = np.array(rows, float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]


def test_label_target_hit():
    # entry open 100, atr 2 -> stop 96 (risk 4), target(1.5x)=103
    rows = [[100, 100, 100, 100]] + [[100, 104, 99, 103]] + [[103, 104, 102, 103]] * 12
    o, h, l, c = _arr(rows)
    win, g, n, xi = label_trade(o, h, l, c, 0, 2.0)
    assert win == 1 and abs(g - 0.75) < 1e-9 and n < g and xi == 1


def test_label_stop_first_when_both():
    rows = [[100, 100, 100, 100]] + [[100, 110, 90, 100]] + [[100, 101, 99, 100]] * 12
    o, h, l, c = _arr(rows)
    win, g, n, xi = label_trade(o, h, l, c, 0, 2.0)
    assert win == 0 and abs(g + 1) < 1e-9


def test_label_time_exit_and_incomplete():
    rows = [[100, 100, 100, 100]] + [[100, 101, 99.5, 100.5]] * 12
    o, h, l, c = _arr(rows)
    win, g, n, xi = label_trade(o, h, l, c, 0, 2.0)
    assert win == 0 and xi == HOLD
    assert label_trade(o, h, l, c, 5, 2.0) is None


def test_walkforward_embargo_no_overlap():
    dates = pd.bdate_range("2021-01-01", periods=1000)
    folds = walk_forward_folds(dates, 5)
    assert len(folds) == 5
    u = list(dates)
    for t0, t1, s0, s1 in folds:
        assert u.index(s0) - u.index(t1) > HOLD
        assert t1 < s0 < s1
    assert all(folds[i][3] < folds[i + 1][2] for i in range(4))
