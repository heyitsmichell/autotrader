from types import SimpleNamespace

from app.fastloop import FastLoop


def _fl():
    return SimpleNamespace(_reject_logged={}, _REJECT_LOG_EVERY_S=FastLoop._REJECT_LOG_EVERY_S)


def test_gap_reject_logged_once_per_30_min():
    fl = _fl()
    chk = lambda r, t: FastLoop._should_log_result(fl, "TXN", r, now=t)
    r1 = "REJECTED (eligibility): Gap filter: TXN gapped 5.20% (prev close $100.00, last $105.20, max 3%)"
    r2 = "REJECTED (eligibility): Gap filter: TXN gapped 5.30% (prev close $100.00, last $105.30, max 3%)"
    assert chk(r1, 0)
    assert not chk(r2, 60)             # same reason, drifting price
    assert chk("REJECTED (eligibility): First 15 minutes", 120)   # reason changed
    assert chk(r1, 1900)               # >30 min later
    assert FastLoop._should_log_result(fl, "TXN", "OK proposal", now=1901)   # non-rejects always log
