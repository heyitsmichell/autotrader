"""Operational fixes: orphaned runs, news back-off, cycle floor."""


def test_orphan_runs_are_closed(test_db):
    rid = test_db.create_run("manual")
    assert test_db.close_orphan_runs() >= 1
    assert test_db.close_orphan_runs() == 0


def test_cycle_floor(test_settings, test_engine):
    assert test_engine.cycle_minutes_effective >= test_settings.llm_min_cycle_minutes
