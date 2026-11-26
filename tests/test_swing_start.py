from app import results


class FakeDB:
    def benchmark_daily(self, *a, **k):
        return [{"day": "2026-01-02", "net_liq": 1_999_999.0}]


def test_swing_start_and_daily():
    st = {k: {"start_capital": 100_000, "start_date": "2026-01-02"} for k in ("momentum", "putwrite", "vaa")}
    assert results.swing_start(FakeDB(), st) == 1_999_999.0 - 300_000
    series = [{"day": "2026-01-02", "swing": 1_700_000.0, "momentum": 100_000, "putwrite": 100_000,
               "vaa": 100_000, "total": 2_000_099.0}]
    starts = results._with_account_starts(series, st, results._starts(series, st, results.SLEEVES), FakeDB())
    d = results.daily_pnl(series, results.SERIES, starts)[0]
    assert d["swing"] == 300_001.0 - 300_000 + 0.0 or d["swing"] == round(1_700_000.0 - 1_699_999.0, 2)
    assert d["total"] == 100.0
