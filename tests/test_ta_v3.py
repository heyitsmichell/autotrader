"""TA scoring v3 (opt-in) and the additive long-horizon features it needs."""
from datetime import datetime, timedelta, timezone

from app import indicators, ta

BASE = {
    "close": 110.0, "sma20": 106.0, "sma50": 100.0, "pct_vs_sma50": 10.0, "atr14": 2.0,
    "atr_pct": 1.8, "adx14": 30.0, "rsi14": 55.0, "sma20_slope_pct": 0.8, "chg_20d_pct": 8.0,
    "pct_from_high10": -3.0, "vol_vs_20d_avg": 1.0, "chg_5d_pct": 2.0,
}
V3F = {**BASE, "sma200": 90.0, "pct_vs_sma200": 22.0, "ret_12_1": 40.0, "ret_126d": 25.0, "vol60": 25.0,
       "vol60_rank": 0.5, "rs_126d": 10.0, "pct_from_high252": -2.0}


def _bars(closes):
    t0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return [{"time": t0 + timedelta(days=i), "open": c, "high": c * 1.01, "low": c * 0.99,
             "close": c, "volume": 1000.0} for i, c in enumerate(closes)]


def test_v2_unchanged_and_default(monkeypatch):
    monkeypatch.delenv("TA_MODEL", raising=False)
    assert ta.model() == "v2"
    # the long-horizon keys must not change the v2 score
    assert ta.score(V3F) == ta.score(BASE) == ta._score_v2(BASE)


def test_v3_only_with_env(monkeypatch):
    monkeypatch.setenv("TA_MODEL", "v3")
    assert ta.model() == "v3"
    assert ta.score(V3F) == ta._score_v3(V3F) != ta._score_v2(V3F)
    monkeypatch.setenv("TA_MODEL", "v1")
    assert ta.model() == "v2"
    monkeypatch.delenv("TA_MODEL")
    assert ta.score(V3F) == ta._score_v2(V3F)


def test_new_features_no_lookahead():
    closes = [100 + (i % 17) * 0.5 + i * 0.1 for i in range(400)]
    bench = _bars([50 + i * 0.05 for i in range(400)])
    full = indicators.feature_series(_bars(closes), bench)
    cut = indicators.feature_series(_bars(closes[:300]), bench[:300])
    for k in ("sma200", "ret_63d", "ret_126d", "ret_12_1", "high252", "vol60", "vol60_rank",
              "pct_from_high252", "rs_63d", "rs_126d"):
        assert full[299][k] == cut[299][k], k


def test_12_1_skips_last_month():
    closes = [100.0] * 253
    closes[-1] = 150.0            # last-day spike must not enter 12-1
    closes[0] = 80.0
    f = indicators.feature_series(_bars(closes))[-1]
    assert abs(f["ret_12_1"] - 25.0) < 0.01       # close[-22]=100 vs close[-253]=80
    assert abs(f["ret_252d"] - 87.5) < 0.01       # plain 252d does include it


def test_components_capped_and_total_clipped():
    extreme = {**V3F, "pct_vs_sma200": 500.0, "ret_12_1": 900.0, "ret_126d": 900.0, "rs_126d": 500.0,
               "pct_from_high252": 0.0, "vol_vs_20d_avg": 3.0, "chg_5d_pct": 5.0}
    total, comps = ta._score_v3(extreme)
    caps = {"Long trend (vs SMA200)": 0.20, "Momentum (12-1 and 6m, vol-adjusted)": 0.40,
            "Relative strength vs SPY (6m)": 0.15, "52-week high proximity": 0.10}
    for c in comps:
        assert abs(c["points"]) <= caps.get(c["name"], 0.15) + 1e-9
    assert -1 <= total <= 1
    assert all("dip-buy" not in c["note"] for c in ta._score_v3({**V3F, "rsi14": 20.0})[1])


def test_missing_history_is_none_safe():
    short = indicators.feature_series(_bars([100 + i for i in range(30)]))[-1]
    for k in ("sma200", "ret_126d", "ret_12_1", "high252", "vol60", "rs_126d", "pct_from_high252"):
        assert short[k] is None
    total, comps = ta._score_v3(short)
    assert -1 <= total <= 1
    assert ta._score_v3({})[0] == 0.0
