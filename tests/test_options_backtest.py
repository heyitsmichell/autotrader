import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from options_entry_backtest import bs_call, bs_delta, strike_for_delta  # noqa: E402


def test_known_value_and_parity():
    S, K, T, s, r = 100, 100, 1.0, 0.2, 0.05
    c = bs_call(S, K, T, s, r)
    assert abs(c - 10.4506) < 1e-3
    assert abs((c - S + K * math.exp(-r * T)) - 5.5735) < 1e-3


def test_delta_solver_and_bounds():
    for d in (0.4, 0.55, 0.7):
        K = strike_for_delta(100, 45 / 365, 0.3, d)
        assert abs(bs_delta(100, K, 45 / 365, 0.3) - d) < 1e-6
    assert bs_call(100, 90, 0, 0.3) == 10
    assert bs_call(100, 150, 0.1, 0.3) >= 0
