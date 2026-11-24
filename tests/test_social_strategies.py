import numpy as np
import pandas as pd
import pytest

from app.rotation_lab import social as S


def fake_panel(closes: dict, n=None):
    """Panel without file IO. closes: name -> array of closes (open = previous close, so gaps are zero)."""
    P = S.Panel.__new__(S.Panel)
    n = len(next(iter(closes.values())))
    P.index = pd.bdate_range("2020-01-01", periods=n)
    P.bars, P.synthetic_until = {}, {}
    for k, v in closes.items():
        c = pd.Series(np.asarray(v, float), index=P.index)
        o = c.shift(1).fillna(c.iloc[0])
        P.bars[k] = pd.DataFrame({"open": o, "close": c, "high": np.maximum(o, c), "low": np.minimum(o, c)})
    cash = pd.Series(100.0, index=P.index)
    P.bars["CASH"] = pd.DataFrame({"open": cash, "close": cash, "high": cash, "low": cash})
    P.cash_ret = pd.Series(0.0, index=P.index)
    return P


def test_synthetic_leverage_math():
    u = pd.Series([0.01, -0.02, 0.005])
    c = pd.Series([0.0001, 0.0001, 0.0001])
    got = S.lev_returns(u, c, 3.0, 0.0189)
    exp = 3 * u - 2 * c - 0.0189 / 252
    assert np.allclose(got, exp)
    # daily rebalancing: +10% then -10% underlying => 3x ends below 1 (volatility decay), not 1.0
    r = S.lev_returns(pd.Series([0.10, -0.10]), pd.Series([0.0, 0.0]), 3.0, 0.0)
    assert (1 + r).prod() == pytest.approx(1.3 * 0.7)
    assert (1 + r).prod() < 1.0


def test_synth_bars_and_splice_continuity():
    idx = pd.bdate_range("2020-01-01", periods=10)
    c = pd.Series(100 * 1.01 ** np.arange(10), index=idx)
    u = pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "close": c})
    syn = S.synth_lev_bars(u, pd.Series(0.0, index=idx), 3.0, 0.0)
    assert syn["close"].iloc[3] / syn["close"].iloc[2] == pytest.approx(1.03)
    real = pd.DataFrame({"open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0}, index=idx[5:])
    sp = S.splice(syn, real)
    assert sp["close"].loc[idx[5]] == pytest.approx(50.0)
    assert sp["close"].iloc[4] == pytest.approx(syn["close"].iloc[4] * 50.0 / syn["close"].iloc[5])


def test_sma_filter_switching_and_hysteresis():
    up = np.linspace(100, 200, 260)
    down = np.linspace(200, 120, 80)
    px = np.r_[up, down]
    P = fake_panel({"SIG": px, "RISK": np.full(len(px), 10.0)})
    W = S.w_trend(P, "SIG", "RISK", "CASH", 200)
    assert W["RISK"].iloc[:199].sum() == 0  # SMA undefined -> flat
    assert W["RISK"].iloc[259] == 1.0  # above SMA while rising
    assert W["RISK"].iloc[-1] == 0.0 and W["CASH"].iloc[-1] == 1.0  # fell below SMA
    # hysteresis: 5% entry buffer delays the entry vs plain
    Wb = S.w_trend(P, "SIG", "RISK", "CASH", 200, up=0.05, dn=0.03)
    assert int(np.argmax(Wb["RISK"].values)) >= int(np.argmax(W["RISK"].values))


def test_rsi2_entry_exit_logic():
    px = np.r_[np.linspace(100, 160, 260), [157, 153, 148]]  # uptrend then a sharp 3-day pullback
    px = np.r_[px, [150, 152, 158, 162, 165]]  # bounce
    c = pd.Series(px)
    r2 = S.rsi_wilder(c, 2)
    sma5 = c.rolling(5).mean()
    sma200 = c.rolling(200).mean()
    ent = ((r2 < 10) & (c > sma200)).values
    ex = (c > sma5).values
    pos = S.toggle(ent, ex)
    first = int(np.argmax(ent))
    assert first >= 260 and pos[first]  # enters on the oversold close
    assert not pos[:first].any()
    # held until close > 5d SMA, then flat
    off = first + int(np.argmax(~pos[first:]))
    assert c.iloc[off] > sma5.iloc[off] and (c.iloc[first:off] <= sma5.iloc[first:off]).all() or off == first + 1
    # wired through the weights builder
    P = fake_panel({"X": px})
    W = S.w_rsi2(P, "X", 10, "sma5", 200)
    assert W["X"].values[first] == 1.0 and W["X"].iloc[:first].sum() == 0


def test_no_lookahead_execution_and_truncation():
    # signal at close t trades at open t+1: weight set on day 5 earns day 6 (open6->open7), never day 5
    n = 12
    px = np.r_[np.full(6, 100.0), 110.0, 121.0, 121.0, 121.0, 121.0, 121.0]
    P = fake_panel({"X": px})
    W = pd.DataFrame(0.0, index=P.index, columns=["X", "CASH"])
    W["CASH"] = 1.0
    W.loc[P.index[5:], "X"] = 1.0
    W.loc[P.index[5:], "CASH"] = 0.0
    r, _ = S.simulate(P, W)
    assert r.iloc[:6].abs().max() == 0.0  # nothing before the fill day
    assert r.iloc[6] > 0  # open6=100 -> open7=110 (open[t]=close[t-1])
    # truncating future data never changes past weights
    base = np.r_[np.linspace(100, 150, 300), np.linspace(150, 110, 100), np.linspace(110, 170, 150)]
    full = fake_panel({"X": base})
    cut = fake_panel({"X": base[:400]})
    for f in (lambda p: S.w_trend(p, "X", "X", "CASH", 200), lambda p: S.w_rsi2(p, "X", 10, "sma5", 200),
              lambda p: S.w_ibs(p, "X", 0.2, 0.8)):
        assert np.allclose(f(full).iloc[:400].values, f(cut).values)


def test_9sig_and_tom_shapes():
    P = fake_panel({"TQQQ": np.linspace(100, 200, 300), "IEF": np.full(300, 100.0)})
    W, flag = S.w_9sig(P, 0.09, 0.6)
    assert np.allclose(W.sum(axis=1), 1.0)
    assert (W[["TQQQ", "IEF"]].values >= -1e-9).all() and flag.any()
    w = S.w_tom(fake_panel({"X": np.linspace(100, 120, 120)}), "X", 3, 3)
    assert 0 < w["X"].mean() < 0.5
