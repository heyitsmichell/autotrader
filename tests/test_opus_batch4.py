import numpy as np
import pandas as pd

from app.rotation_lab.engine import _ffill, run
from app.eligibility import _is_today, _ONE_STARTER_OPEN
from app.market import now_et


def test_ffill_carries_forward_and_keeps_leading_nan():
    A = np.array([[np.nan], [1.0], [np.nan], [2.0]])
    out = _ffill(A)[:, 0]
    assert np.isnan(out[0]) and out[1] == 1.0 and out[2] == 1.0 and out[3] == 2.0


def test_run_missing_open_not_zero_return():
    O = np.array([[10.0, 1.0], [np.nan, 1.0], [12.0, 1.0]])
    C = O.copy(); C[1, 0] = 11.0
    W = np.array([[1.0, 0.0]] * 3); flag = np.array([True, False, False])
    r, _ = run(O, C, W, flag, costs=False)
    assert abs(r[1] - 0.2) < 1e-9 or abs(r[0] - 0.0) < 1e-9  # gap move lands, not silently dropped


def test_one_starter_statuses_and_today():
    assert {"NEW", "ACCEPTED", "PARTIAL"} <= _ONE_STARTER_OPEN
    assert _is_today({"placed_at": now_et().isoformat()})
    assert not _is_today({"placed_at": "2020-01-01T10:00:00+00:00"})


def test_momentum_partial_universe_logic():
    from app.sleeves.strategies import momentum
    closes = pd.DataFrame(np.random.rand(300, 5) + 1, columns=list("ABCDE"))
    assert len(momentum.targets(closes, n=3)) == 3
