from datetime import datetime, timedelta, timezone

import pytest

from app import scheduler
from app.models import Repo


@pytest.fixture(autouse=True)
async def _scheduler_lifecycle():
    scheduler.start()
    yield
    scheduler.shutdown()


def _job_ids():
    return {j.id for j in scheduler._scheduler.get_jobs()}


def _job(repo_id: str, branch: str):
    return scheduler._scheduler.get_job(scheduler._job_id(repo_id, branch))


def _hours_until(when) -> float:
    return (when - datetime.now(timezone.utc)).total_seconds() / 3600


def _repo(repo_id: str, *, branch="main", last_scan=None, interval=1) -> Repo:
    return Repo(
        id=repo_id,
        url=f"https://x/{repo_id}",
        branches=[branch],
        interval_days=interval,
        last_scan_by_branch={branch: last_scan} if last_scan else {},
    )


@pytest.mark.asyncio
async def test_sync_repo_creates_one_job_per_branch():
    repo = Repo(id="multi", url="https://x/multi", branches=["develop", "release/6.1"])
    scheduler.sync_repo(repo)
    assert scheduler._job_id("multi", "develop") in _job_ids()
    assert scheduler._job_id("multi", "release/6.1") in _job_ids()


@pytest.mark.asyncio
async def test_sync_repo_drops_jobs_for_removed_branches():
    repo = Repo(id="shrink", url="https://x/shrink", branches=["a", "b", "c"])
    scheduler.sync_repo(repo)
    assert {scheduler._job_id("shrink", b) for b in ("a", "b", "c")} <= _job_ids()

    smaller = repo.model_copy(update={"branches": ["a"]})
    scheduler.sync_repo(smaller)
    ids = _job_ids()
    assert scheduler._job_id("shrink", "a") in ids
    assert scheduler._job_id("shrink", "b") not in ids
    assert scheduler._job_id("shrink", "c") not in ids


@pytest.mark.asyncio
async def test_unsync_repo_removes_every_branch_job():
    repo = Repo(id="gone", url="https://x/gone", branches=["a", "b"])
    scheduler.sync_repo(repo)
    scheduler.unsync_repo("gone")
    ids = _job_ids()
    assert scheduler._job_id("gone", "a") not in ids
    assert scheduler._job_id("gone", "b") not in ids


@pytest.mark.asyncio
async def test_unrelated_repo_jobs_survive_unsync():
    a = Repo(id="repo-a", url="https://x/a", branches=["main"])
    b = Repo(id="repo-b", url="https://x/b", branches=["main"])
    scheduler.sync_repo(a)
    scheduler.sync_repo(b)
    scheduler.unsync_repo("repo-a")
    assert scheduler._job_id("repo-b", "main") in _job_ids()


# --------------------------------------------------------------------------- #
# First-fire timing: the schedule is anchored to the last completed scan, not   #
# to when the job happened to be (re)created.                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_first_run_is_one_interval_after_the_last_scan():
    """A branch scanned 6h ago on a daily interval is due in ~18h — not in 24h,
    which is what an unanchored interval trigger would have picked."""
    last = datetime.now(timezone.utc) - timedelta(hours=6)
    scheduler.sync_repo(_repo("anchored", last_scan=last))
    assert _hours_until(_job("anchored", "main").next_run_time) == pytest.approx(18, abs=0.1)


@pytest.mark.asyncio
async def test_restart_does_not_push_the_schedule_out():
    """The regression: every sync (startup, repo edit) used to restart the
    interval from now, so a daily job on a frequently restarted backend never
    came due. Re-syncing must leave the due time where it was."""
    repo = _repo("restarts", last_scan=datetime.now(timezone.utc) - timedelta(hours=20))
    scheduler.sync_repo(repo)
    first = _job("restarts", "main").next_run_time
    for _ in range(5):  # five more restarts / edits
        scheduler.sync_repo(repo)
    assert _hours_until(_job("restarts", "main").next_run_time) == pytest.approx(4, abs=0.1)
    assert abs((_job("restarts", "main").next_run_time - first).total_seconds()) < 1


@pytest.mark.asyncio
@pytest.mark.parametrize("last_scan", [
    datetime(2026, 1, 1, tzinfo=timezone.utc),   # long overdue
    None,                                         # never scanned
])
async def test_overdue_branches_are_caught_up_promptly(last_scan):
    scheduler.sync_repo(_repo("overdue", last_scan=last_scan))
    due_in = (_job("overdue", "main").next_run_time - datetime.now(timezone.utc)).total_seconds()
    assert 0 < due_in <= scheduler._CATCHUP_DELAY + 1


@pytest.mark.asyncio
async def test_a_branch_scanning_now_is_scheduled_one_interval_out():
    """A new repo's first scan starts at once; a catch-up 30 s later would
    scan it again (and spend the workspace's AI quota twice)."""
    scheduler.sync_repo(_repo("fresh", interval=7), scanning_now={"main"})
    assert _hours_until(_job("fresh", "main").next_run_time) == pytest.approx(7 * 24, abs=0.1)


@pytest.mark.asyncio
async def test_a_due_scan_skips_a_branch_that_is_already_queued_or_scanning(monkeypatch):
    from app import audit, scan_queue, storage
    storage.upsert_repo(_repo("busy"))
    monkeypatch.setattr(scan_queue, "enqueue", lambda repo, branch, trigger="manual": False)
    await scheduler._scan_job("busy", "main")
    assert audit.events("local") == []  # nothing was queued, so nothing is recorded


@pytest.mark.asyncio
async def test_a_due_scan_is_queued_and_recorded(monkeypatch):
    from app import audit, scan_queue, storage
    storage.upsert_repo(_repo("due"))
    queued = []
    monkeypatch.setattr(scan_queue, "enqueue",
                        lambda repo, branch, trigger="manual": queued.append((repo.id, branch, trigger)) or True)
    await scheduler._scan_job("due", "main")
    assert queued == [("due", "main", "scheduled")]
    assert [e["action"] for e in audit.events("local")] == ["scan.scheduled"]


@pytest.mark.asyncio
async def test_overdue_branches_are_staggered_not_stampeded():
    """Each catch-up is a clone plus AI enrichment, so several overdue branches
    must not all fire at the same instant."""
    repo = Repo(
        id="many", url="https://x/many", branches=["a", "b", "c"], interval_days=1,
    )
    scheduler.sync_repo(repo)
    runs = [_job("many", b).next_run_time for b in ("a", "b", "c")]
    gaps = [(runs[i + 1] - runs[i]).total_seconds() for i in range(2)]
    assert all(g == pytest.approx(scheduler._CATCHUP_STAGGER, abs=1) for g in gaps)


@pytest.mark.asyncio
async def test_naive_last_scan_is_treated_as_utc():
    """Legacy repos.json entries were written without a timezone; comparing one
    against an aware `now` would raise and take the whole sync down."""
    naive = datetime.utcnow() - timedelta(hours=6)
    scheduler.sync_repo(_repo("legacy", last_scan=naive))
    assert _hours_until(_job("legacy", "main").next_run_time) == pytest.approx(18, abs=0.1)


@pytest.mark.asyncio
async def test_late_runs_are_never_silently_skipped():
    """APScheduler's default 1s misfire grace would drop a scan whose fire time
    slipped (busy loop, suspended laptop) instead of running it late."""
    scheduler.sync_repo(_repo("late", last_scan=datetime.now(timezone.utc)))
    job = _job("late", "main")
    assert job.misfire_grace_time is None
    assert job.coalesce is True


@pytest.mark.asyncio
async def test_interval_change_takes_effect_from_the_last_scan():
    last = datetime.now(timezone.utc) - timedelta(days=2)
    scheduler.sync_repo(_repo("retimed", last_scan=last, interval=7))
    assert _hours_until(_job("retimed", "main").next_run_time) == pytest.approx(120, abs=0.1)
