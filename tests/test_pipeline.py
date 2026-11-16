from datetime import datetime, timezone

from pipeline.run import cron_due, load_jobs


def t(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_cron_daily_and_weekly():
    assert cron_due("17 6 * * *", t(2026, 11, 16, 6, 17))
    assert not cron_due("17 6 * * *", t(2026, 11, 16, 6, 18))
    assert cron_due("23 7 * * 1", t(2026, 11, 16, 7, 23))      # 16 Nov 2026 is a Monday
    assert not cron_due("23 7 * * 1", t(2026, 11, 17, 7, 23))


def test_cron_steps_ranges_lists():
    assert cron_due("*/15 * * * *", t(2026, 1, 1, 3, 45))
    assert not cron_due("*/15 * * * *", t(2026, 1, 1, 3, 46))
    assert cron_due("0 9-17/4 * * 1-5", t(2026, 11, 20, 13, 0))
    assert cron_due("5 1,13 * * 0", t(2026, 11, 22, 13, 5))     # Sunday


def test_jobs_reference_existing_prompts_and_dirs():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name, job in load_jobs().items():
        assert (root / job["prompt"]).exists(), name
        assert (root / job["cwd"]).is_dir(), name
        assert job.get("service") or job.get("schedule"), name
