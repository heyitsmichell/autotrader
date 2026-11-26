from app.ml import scorecard_extras, permutation_importance


def test_extras_basic():
    ps = [0.9, 0.8, 0.6, 0.3, 0.2, 0.01]
    ys = [1, 0, 1, 1, 0, 0]
    r = scorecard_extras(ps, ys, 0.5)
    assert r["precision"] == 2 / 3
    assert r["recall"] == 2 / 3
    assert abs(r["f1"] - 2 / 3) < 1e-9
    assert r["saturation_pct"] == round(1 / 6, 4)
    assert 0 <= r["calibration_error"] <= 1


def test_perfect_calibration_and_empty():
    assert scorecard_extras([], [], 0.5) == {}
    r = scorecard_extras([0.5] * 10, [1] * 5 + [0] * 5, 0.5)
    assert r["calibration_error"] == 0.0


def test_permutation_importance_ranks_signal():
    xs = [[float(i % 2), float(i % 3)] for i in range(200)]
    ys = [float(x[0]) for x in xs]
    out = permutation_importance(lambda x: [r[0] for r in x], xs, ys, ["sig", "noise"])
    assert out[0]["feature"] == "sig" and out[0]["importance"] > 0
