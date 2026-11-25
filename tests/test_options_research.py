import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import options_research as o  # noqa: E402


def test_put_call_parity():
    S, K, T, s, r = 100.0, 95.0, 0.25, 0.3, 0.03
    c, p = o.bs_call(S, K, T, s, r), o.bs_put(S, K, T, s, r)
    assert abs(c - p - (S - K * math.exp(-r * T))) < 1e-9


def test_put_delta_strike_below_spot():
    K = o.strike_for_put_delta(100.0, 30 / 365, 0.2, 0.30, 0.03)
    assert K < 100.0
    assert o.bs_put(100.0, K, 30 / 365, 0.2, 0.03) > 0


def test_no_data_after_holdout():
    assert o.END < o.HOLDOUT == date(2025, 11, 23)
    mk = o.Mkt()
    assert max(mk.dates) < o.HOLDOUT
