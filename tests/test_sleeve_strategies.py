from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.sleeves import registry
from app.sleeves.strategies import momentum, putwrite, rebalance, vaa

ROOT = Path(__file__).parent.parent


def test_registry():
    assert [s.id for s in registry.SLEEVES] == ["swing", "momentum", "putwrite", "vaa"]
    assert registry.get("vaa").code == "v"
    assert registry.by_code("p").id == "putwrite"
    with pytest.raises(KeyError):
        registry.get("x")
    c = registry.coid_for("momentum")
    assert len(c) == 32 and c.startswith("sm") and c == c.lower()
    assert registry.sleeve_from_coid(c) == "momentum"
    assert registry.sleeve_from_coid("abc") is None


def _closes(ncols=12, rows=300):
    idx = pd.bdate_range("2020-01-01", periods=rows)
    return pd.DataFrame({f"S{i}": 100 * (1 + 0.0005 * (i + 1)) ** np.arange(rows) for i in range(ncols)}, index=idx)


def test_momentum():
    c = _closes()
    t = momentum.targets(c, 10)
    assert set(t) == {f"S{i}" for i in range(2, 12)}
    assert sum(t.values()) == pytest.approx(1)
    mom = (c.shift(21) / c.shift(252) - 1).iloc[-1]
    assert set(t) == set(mom.nlargest(10).index)
    assert momentum.targets(c.iloc[:100], 10) == {}
    c2 = c.copy()
    c2.iloc[:, :5] = np.nan
    assert momentum.targets(c2, 10) == {}
    assert momentum.targets(c.iloc[0:0]) == {}


def _vaa_closes(rate):
    idx = pd.bdate_range("2020-01-01", periods=300)
    cols = vaa.G12 + ["IEF", "SHY"]
    return pd.DataFrame({s: 100 * (1 + rate.get(s, 0.0003)) ** np.arange(300) for s in cols}, index=idx)


def test_vaa_risk_on_and_off():
    r = {"QQQ": 0.002, "SPY": 0.001}
    t = vaa.targets(_vaa_closes(r))
    assert t == pytest.approx({"QQQ": 0.5, "SPY": 0.5})
    off = {s: -0.001 for s in vaa.G12}
    off.update({"IEF": 0.0005})
    assert vaa.targets(_vaa_closes(off)) == {"IEF": 1.0}
    assert vaa.targets(_vaa_closes(r).iloc[:100]) == {}
    assert vaa.targets(_vaa_closes(r).drop(columns=["SHY"])) == {}


def _chain():
    rows = []
    for exp, k, dl, bid in [(date(2025, 1, 31), 90, -0.2, 1), (date(2025, 1, 31), 95, -0.31, 2),
                            (date(2025, 1, 31), 98, -0.45, 0), (date(2025, 1, 10), 95, -0.3, 1),
                            (date(2025, 2, 21), 95, -0.3, 1)]:
        rows.append(dict(symbol=f"P{k}", strike=k, expiration=exp, option_type="PUT", delta=dl, bid=bid, ask=bid + 1))
    rows.append(dict(symbol="C", strike=100, expiration=date(2025, 1, 31), option_type="CALL", delta=0.5, bid=1, ask=2))
    return rows, date(2025, 1, 1)


def test_pick_contract():
    rows, d = _chain()
    p = putwrite.pick_contract(rows, 100, d)
    assert p["strike"] == 95 and p["dte"] == 30
    for r in rows:
        r["delta"] = None
    assert putwrite.pick_contract(rows, 100, d)["strike"] == 95  # closest to 96
    assert putwrite.pick_contract([], 100, d) is None


def test_put_helpers():
    assert putwrite.contracts_for(25000, 100) == 2
    assert putwrite.contracts_for(5000, 100) == 0
    put = {"strike": 100, "expiration": date(2025, 1, 10)}
    t = date(2025, 1, 10)
    assert putwrite.should_close(put, 99, t, datetime(2025, 1, 10, 15, 45))
    assert not putwrite.should_close(put, 99, t, datetime(2025, 1, 10, 12, 0))
    assert not putwrite.should_close(put, 101, t, datetime(2025, 1, 10, 15, 45))
    assert putwrite.should_close(put, 99, date(2025, 1, 11), datetime(2025, 1, 11, 9, 0))
    assert putwrite.is_expired(put, t, datetime(2025, 1, 10, 16, 0))
    assert not putwrite.is_expired(put, t, datetime(2025, 1, 10, 15, 59))
    assert putwrite.is_expired(put, date(2025, 1, 11), datetime(2025, 1, 11, 9, 0))


def test_rebalance_diff():
    px = {"A": 100.0, "B": 50.0, "C": 10.0, "Z": 0}
    out = rebalance.diff({"C": 5, "A": 10}, {"A": 0.5, "B": 0.5, "Z": 0.1}, 10000, px)
    assert out == [("C", "SELL", 5), ("A", "BUY", 40), ("B", "BUY", 100)]
    assert rebalance.diff({"A": 49}, {"A": 0.5}, 10000, px) == []
    assert rebalance.diff({"C": 5}, {}, 10000, px) == [("C", "SELL", 5)]
    assert rebalance.diff({"A": 45}, {"A": 0.5}, 10000, px, min_trade_usd=0, band_pct=10) == []
    assert rebalance.diff({}, {}, 1000, px) == []


def test_chunks():
    assert rebalance.chunks(0, 10, 100, 5) == []
    assert rebalance.chunks(10, 10, 0, 0) == [10]
    assert rebalance.chunks(10, 10, 0, 4) == [4, 4, 2]
    assert rebalance.chunks(10, 10, 30, 0) == [3, 3, 3, 1]
    assert rebalance.chunks(3, 1000, 10, 0) == [1, 1, 1]


@pytest.mark.skipif(not (ROOT / "data/bars_yahoo_ext/SPY.csv").exists(), reason="no yahoo ext data")
def test_vaa_regression_real_data():
    syms = sorted(set(vaa.G12 + vaa.DEFENSIVE))
    cols = {}
    for s in syms:
        df = pd.read_csv(ROOT / f"data/bars_yahoo_ext/{s}.csv", index_col=0, parse_dates=True)
        cols[s] = df["close"][df.index <= "2025-11-21"]  # locked holdout: never load later rows
    c = pd.DataFrame(cols).dropna(how="all")
    sub = c.index[c.index <= "2025-10-31"]
    me = sub[sub.to_period("M") == sub[-1].to_period("M")][-1]
    t = vaa.targets(c.loc[:me])
    assert t and sum(t.values()) == pytest.approx(1)
    assert set(t) <= set(syms)
