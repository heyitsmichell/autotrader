import numpy as np
import pandas as pd
import pytest

from app.rotation_lab import momentum_variants as mv


def _prices(n=800, m=6, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2012-01-02", periods=n)
    r = rng.normal(0.0004, 0.015, (n, m))
    return pd.DataFrame(100 * np.cumprod(1 + r, axis=0), index=idx, columns=list("ABCDEF")[:m])


@pytest.mark.parametrize("name", sorted(mv.SCORES))
def test_no_lookahead(name):
    c = _prices()
    t = 650
    base = mv.SCORES[name](c)
    c2 = c.copy()
    rng = np.random.default_rng(5)
    c2.iloc[t + 1:] = c2.iloc[t + 1:] * rng.uniform(0.5, 2.0, c2.iloc[t + 1:].shape)
    alt = mv.SCORES[name](c2)
    pd.testing.assert_frame_equal(base.iloc[: t + 1], alt.iloc[: t + 1])


def test_no_lookahead_seasonal_on_month_end_row():
    idx = pd.bdate_range("2008-01-01", "2020-12-31")
    rng = np.random.default_rng(1)
    c = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0, .01, (len(idx), 3)), axis=0), index=idx, columns=list("XYZ"))
    fl = np.where(mv.rebal_flags(idx, "monthly"))[0]
    t = fl[100]
    c2 = c.copy(); c2.iloc[t + 1:] *= 3.0
    a, b = mv.seasonal(c).iloc[t], mv.seasonal(c2).iloc[t]
    assert a.notna().all() and np.allclose(a, b)


def test_gates_no_lookahead():
    c = _prices(1700, 4)
    t = 1500
    c2 = c.copy(); c2.iloc[t + 1:] *= 0.3
    assert (mv.bear_state(c["A"])[: t + 1] == mv.bear_state(c2["A"])[: t + 1]).all()
    assert (mv.breadth_low(c)[: t + 1] == mv.breadth_low(c2)[: t + 1]).all()


def test_rebalance_flags():
    idx = pd.bdate_range("2020-01-01", "2021-12-31")
    mo = mv.flags_for(idx, "monthly")
    assert mo.sum() == 23  # 24 months, last row has no next bar
    q = idx[mv.flags_for(idx, "quarterly")]
    assert set(q.month) == {2, 5, 8, 11}
    assert len(q) == 8  # Nov-2021 included (Dec rows follow)
    for d in q:  # last trading day of its month
        assert idx[idx.get_loc(d) + 1].month != d.month
    w = idx[mv.flags_for(idx, "weekly")]
    assert all(d.dayofweek == 4 or idx[idx.get_loc(d) + 1].isocalendar()[1] != d.isocalendar()[1] for d in w)
    assert 100 <= len(w) <= 105


def test_fip_id_sign_and_math():
    n = 400
    idx = pd.bdate_range("2015-01-01", periods=n)
    # smooth uptrend: all positive daily returns -> ID = 1*(0-1) = -1 ; choppy up: mixed
    smooth = pd.Series(100 * 1.001 ** np.arange(n), index=idx)
    steps = np.where(np.arange(n) % 2 == 0, 1.0 + 0.0102, 1.0 - 0.0100)
    choppy = pd.Series(100 * np.cumprod(steps), index=idx)
    down = pd.Series(100 * 0.999 ** np.arange(n), index=idx)
    ids = mv.fip_id(pd.DataFrame({"smooth": smooth, "choppy": choppy, "down": down}))
    last = ids.iloc[-1]
    assert last["smooth"] == pytest.approx(-1.0)
    assert last["down"] == pytest.approx(-1.0)          # sign(PRET)=-1 * (1 - 0) = -1
    assert -0.1 < last["choppy"] < 0.1 and last["choppy"] > last["smooth"]
    # hand-computed on the window: 231 returns, shift 21
    r = choppy.pct_change().shift(21).iloc[-231:]
    exp = np.sign(choppy.iloc[-22] / choppy.iloc[-253] - 1) * ((r < 0).mean() - (r > 0).mean())
    assert last["choppy"] == pytest.approx(exp)


def test_fip_select_prefers_smooth():
    mom = np.array([0.9, 0.8, 0.7, 0.6, 0.1])
    ident = np.array([0.5, -0.2, 0.1, -0.5, -0.9])
    sel = mv.fip_select(mom, ident, 2)       # top 4 by mom (0..3), two lowest ID: idx 3 then 1
    assert list(sel) == [3, 1]


def test_deflation_gets_combined_trial_count(monkeypatch):
    seen = {}

    def fake(r, n_trials, sr_var):
        seen["n"] = n_trials
        return 0.5
    monkeypatch.setattr(mv, "deflated_sharpe", fake)
    mv.dsr(pd.Series(np.zeros(100)), 816, 50, 1e-3)
    assert seen["n"] == 866


def test_registry_row_count(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("a,b\n1,2\n3,4\n")
    assert mv.n_registry_rows(p) == 2


def test_bootstrap_paired_identical_is_zero():
    r = pd.Series(np.random.default_rng(0).normal(0.0005, 0.01, 1000))
    pt, lo, hi = mv.block_boot_dsharpe(r, r, n=1000)
    assert pt == 0 and lo == 0 and hi == 0
