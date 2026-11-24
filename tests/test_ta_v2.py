"""TA scoring model v2: range, grouping, ADX scaling, RSI consistency, v1 retired."""
from app import ta

BASE = {
    "close": 110.0, "sma20": 106.0, "sma50": 100.0, "pct_vs_sma50": 10.0, "atr14": 2.0,
    "atr_pct": 1.8, "adx14": 30.0, "rsi14": 55.0, "sma20_slope_pct": 0.8, "chg_20d_pct": 8.0,
    "pct_from_high10": -3.0, "vol_vs_20d_avg": 1.0, "chg_5d_pct": 2.0,
}


def test_default_model_is_v2(monkeypatch):
    monkeypatch.delenv("TA_MODEL", raising=False)
    assert ta.model() == "v2"
    assert ta.score(BASE) == ta.score(BASE, "v2")


def test_env_v2_still_accepted(monkeypatch):
    monkeypatch.setenv("TA_MODEL", "V2")
    assert ta.model() == "v2"
    assert ta.score(BASE) == ta.score(BASE, "v2")


def test_v1_maps_to_v2_with_one_time_warning(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("TA_MODEL", "v1")
    monkeypatch.setattr(ta, "_warned_v1", False)
    with caplog.at_level(logging.WARNING, logger="app.ta"):
        assert ta.model() == "v2"
        assert ta.model() == "v2"
        assert ta.score(BASE) == ta.score(BASE, "v2")
        assert ta.score(BASE, "v1") == ta.score(BASE, "v2")
    warnings = [r for r in caplog.records if "TA_MODEL=v1" in r.getMessage()]
    assert len(warnings) == 1
    assert not hasattr(ta, "_score_v1")


def test_v2_range_and_components():
    total, comps = ta.score(BASE, "v2")
    assert -1 <= total <= 1 and total > 0.3
    assert len(comps) >= 4
    assert all({"name", "value", "points", "note"} <= set(c) for c in comps)


def test_bearish_scores_negative():
    f = {**BASE, "close": 90.0, "sma20": 94.0, "pct_vs_sma50": -10.0, "sma20_slope_pct": -0.8,
         "chg_20d_pct": -8.0, "chg_5d_pct": -3.0, "pct_from_high10": -9.0, "rsi14": 45.0}
    assert ta.score(f, "v2")[0] < -0.2


def test_distance_is_scaled_by_volatility():
    near = {**BASE, "pct_vs_sma50": 0.5}
    far = {**BASE, "pct_vs_sma50": 8.0}
    assert ta.score(far, "v2")[0] > ta.score(near, "v2")[0]
    calm = {**BASE, "atr_pct": 0.8}
    wild = {**BASE, "atr_pct": 4.0}
    assert ta.score(calm, "v2")[0] > ta.score(wild, "v2")[0]


def test_weak_trend_counts_less():
    strong = ta.score({**BASE, "adx14": 30.0}, "v2")[0]
    weak = ta.score({**BASE, "adx14": 12.0}, "v2")[0]
    assert weak < strong


def test_rsi_oversold_depends_on_trend():
    up = {**BASE, "rsi14": 27.0}
    down = {**BASE, "close": 90.0, "sma20": 94.0, "pct_vs_sma50": -10.0, "rsi14": 27.0}
    rsi_up = next(c for c in ta.score(up, "v2")[1] if c["name"] == "RSI14")
    rsi_down = next(c for c in ta.score(down, "v2")[1] if c["name"] == "RSI14")
    assert rsi_up["points"] > 0 > rsi_down["points"]


def test_volume_confirms_direction():
    hi_up = next(c for c in ta.score({**BASE, "vol_vs_20d_avg": 2.0}, "v2")[1]
                 if c["name"] == "Volume confirmation")
    hi_dn = next(c for c in ta.score({**BASE, "vol_vs_20d_avg": 2.0, "chg_5d_pct": -2.0}, "v2")[1]
                 if c["name"] == "Volume confirmation")
    assert hi_up["points"] == 0.1 and hi_dn["points"] == -0.1


def test_missing_data_does_not_crash():
    total, comps = ta.score({"close": 100.0}, "v2")
    assert total == 0.0 and comps == []
    assert ta.score({}, "v2") == (0.0, [])