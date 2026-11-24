import numpy as np
import pandas as pd

from app.research import harness, leaderboard, variants


def _dates(n=1500):
    return list(pd.bdate_range("2019-01-01", periods=n).date)


def test_holdout_rows_never_in_research_folds():
    d = _dates()
    research, holdout, cutoff = harness.split_dates(d)
    assert holdout and min(holdout) >= cutoff and max(research) < cutoff
    folds = harness.make_folds(research)
    assert len(folds) == harness.N_FOLDS
    for te, s0, s1 in folds:
        assert s1 < cutoff and s0 < cutoff


def test_folds_respect_embargo():
    research, _, _ = harness.split_dates(_dates())
    u = sorted(research)
    for te, s0, s1 in harness.make_folds(research):
        gap = u.index(s0) - u.index(te)
        assert gap >= harness.EMBARGO + 1


def test_leaderboard_append_and_rank(tmp_path):
    p = tmp_path / "lb.csv"

    def res(name, pen):
        return {"variant": name, "config_hash": "h" + name, "scope": "research", "n_trials": 3, "trades": 100,
                "exp_R": pen, "se": 0.01, "penalized": pen, "all_positive": True,
                "folds": [{"exp_R": 0.1, "trades": 20}] * 5, "max_dd": 0.1, "seconds": 1}
    leaderboard.append([res("a", 0.05), res("b", 0.2)], p)
    leaderboard.append([res("c", 0.1)], p)
    df = leaderboard.load(p)
    assert len(df) == 3
    r = leaderboard.rank(df)
    assert list(r.variant) == ["b", "c", "a"]
    assert "b" in leaderboard.table(r, "a")


def test_promotion_rule():
    champ = {"penalized": 0.05, "fold_exp_R": [0.1] * 5}
    good = {"penalized": 0.08, "fold_exp_R": [0.1, 0.2, 0.05, 0.3, 0.1]}
    assert leaderboard.promotable(good, champ)[0]
    assert not leaderboard.promotable({**good, "penalized": 0.04}, champ)[0]
    assert not leaderboard.promotable({**good, "fold_exp_R": [0.1, -0.01, 0.1, 0.1, 0.1]}, champ)[0]
    assert leaderboard.promotable(good, champ, 0.2, 0.1)[0]
    assert not leaderboard.promotable(good, champ, 0.05, 0.1)[0]
    assert not leaderboard.promotable(good, champ, 0.2, None)[0]


def test_penalized_score_shrinks_with_trials():
    a = harness.penalized_score(400, 40.0, 500.0, 2)[2]
    b = harness.penalized_score(400, 40.0, 500.0, 50)[2]
    assert b < a < 0.1


def test_ml_filter_trains_only_on_past():
    r = np.random.default_rng(0)
    n = 6000
    dts = pd.bdate_range("2020-01-01", periods=1500)
    di = r.integers(0, 1490, n)
    D = pd.DataFrame({"x": r.normal(size=n), "win": r.integers(0, 2, n), "sym": "AAA",
                      "date": dts[di], "exit_date": dts[di + 8]})
    seen = []

    def fit(kind, Xtr, ytr, Xte, seed=0):
        seen.append(len(Xtr))
        return np.random.default_rng(1).normal(size=len(Xte))

    s0 = dts[1000].date()
    tr_end = dts[1000 - harness.EMBARGO - 1].date()
    passed, audit = harness.ml_pass_map(D, ["x"], [(tr_end, s0, dts[1200].date())], fit)
    assert seen and audit
    max_date, max_exit, start = audit[0]
    assert max_date <= tr_end and max_exit < s0 and max_date < start
    assert all(d >= s0 for d, _ in passed)


def test_registry_has_champion_and_unique_hashes():
    assert variants.CHAMPION in variants.REGISTRY
    hs = [v.config_hash() for v in variants.REGISTRY.values()]
    assert len(set(hs)) == len(hs)
