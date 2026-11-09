"""APScheduler wiring — one interval job per (repo, branch), re-synced on repo
changes.

Jobs live in memory only, so the schedule is rebuilt from scratch on every
startup and on every repo edit. The rebuild therefore anchors each job's first
fire to the branch's **persisted** `last_scan_by_branch`, not to the moment the
job was created: an interval trigger left to itself starts counting from *now*,
which meant a restart (or an edit) silently pushed the next run a full interval
into the future. A daily scan on a machine restarted more than once a day never
came due at all. Anything already past due is scanned shortly after startup
rather than waiting out another interval.

Note (v1, local deploy): jobs only fire while the backend process is running.
That's fine for a dev machine left running; a systemd/launchd unit or the
Docker + `claude setup-token` path would make it survive reboots.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import accounts, analytics, audit, housekeeping, scan_queue, storage, uploads
from .models import Repo

log = logging.getLogger("packages-tracker.scheduler")
_scheduler: AsyncIOScheduler | None = None
_JOB_PREFIX = "scan:"

# Overdue branches don't all fire at once on startup: each scan is a clone plus
# AI enrichment, so they're spread out (and given a moment for the app to finish
# coming up) instead of stampeding the host.
_CATCHUP_DELAY = 30       # seconds after sync before the first catch-up scan
_CATCHUP_STAGGER = 120    # seconds between consecutive catch-up scans


def _job_id(repo_id: str, branch: str) -> str:
    return f"{_JOB_PREFIX}{repo_id}::{branch}"


def _due_at(repo: Repo, branch: str, now: datetime) -> datetime | None:
    """When this branch next comes due — one interval after its last completed
    scan. None means it's already overdue (or has never been scanned), so the
    caller should schedule a catch-up instead of waiting."""
    last = repo.last_scan_by_branch.get(branch)
    if last is None:
        return None
    if last.tzinfo is None:  # legacy repos.json entries were written naive-UTC
        last = last.replace(tzinfo=timezone.utc)
    due = last + timedelta(days=repo.interval_days)
    return due if due > now else None


async def _scan_job(repo_id: str, branch: str) -> None:
    repo = storage.get_repo(repo_id)
    if repo is None or branch not in repo.branches:
        return
    if repo.source == "upload" and not uploads.has_upload(repo_id, branch):
        return  # nothing to scan until a zip is uploaded for this version
    if housekeeping.inactive(repo.workspace_id):
        # nobody has used this workspace for a month: don't spend on it
        housekeeping.mark_paused(repo.workspace_id)
        log.info("scheduled scan for %s @ %s skipped: its workspace is inactive", repo_id, branch)
        return
    if not scan_queue.enqueue(repo, branch, "scheduled"):
        # already queued or running (e.g. a manual scan got there first)
        log.info("scheduled scan for %s @ %s skipped: already queued or scanning", repo_id, branch)
        return
    log.info("scheduled scan queued for %s @ %s", repo_id, branch)
    audit.record("scan.scheduled", workspace_id=repo.workspace_id, actor_kind="scheduler",
                 target_type="repo", target_id=repo.id, branch=branch)


_UPKEEP_JOB = "upkeep"


def _upkeep_job() -> None:
    """Daily: history retention (housekeeping.py). Runs in a worker thread."""
    housekeeping.prune_history()


_SAMPLE_JOB = "system-sample"


def _sample_job() -> None:
    """Hourly: a reading of the server's health for the admin analytics.
    Runs in a worker thread."""
    analytics.record_sample()


_TRIAL_JOB = "trial-expire"


def _trial_job() -> None:
    """Hourly: delete the free scans nobody saved (trial.py). Runs in a
    worker thread."""
    from . import trial  # (it imports the API's deletion helpers, which import this module)
    trial.expire()


def resync_workspaces(workspace_ids: list[str]) -> None:
    """Reschedule these workspaces' repos, e.g. after a pause ends: overdue
    branches get their catch-up scans now rather than an interval from now."""
    for ws in workspace_ids:
        for repo in storage.load_repos(ws):
            sync_repo(repo)


def _requeue_trials() -> None:
    """A restart empties the queue: free scans that never ran go back in, but
    without the visitor's token, which only lived in memory (a private repo's
    scan then fails with a clear clone error)."""
    trial_ws = accounts.trial_workspace_ids()
    for repo in storage.load_repos():
        if repo.workspace_id in trial_ws:
            for branch in repo.branches:
                if branch not in repo.last_scan_by_branch:
                    scan_queue.enqueue_trial(repo, branch, None)


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
        _scheduler.start()
        sync_all()
        _requeue_trials()
        _scheduler.add_job(
            _upkeep_job, "interval", hours=24, id=_UPKEEP_JOB, replace_existing=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            coalesce=True, misfire_grace_time=None, max_instances=1,
        )
        _scheduler.add_job(
            _sample_job, "interval", hours=1, id=_SAMPLE_JOB, replace_existing=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
            coalesce=True, misfire_grace_time=None, max_instances=1,
        )
        _scheduler.add_job(
            _trial_job, "interval", hours=1, id=_TRIAL_JOB, replace_existing=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            coalesce=True, misfire_grace_time=None, max_instances=1,
        )
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def sync_repo(repo: Repo, *, catchup_slot: int = 0, scanning_now: set[str] | frozenset[str] = frozenset()) -> int:
    """Add/replace the interval job for each of the repo's branches, and drop
    jobs for branches no longer in `repo.branches` (e.g. removed on edit).

    `scanning_now`: branches whose scan the caller is starting right now (a
    new repo, an uploaded zip). Their schedule starts one interval later.
    Without this, a never-scanned branch gets a catch-up 30 s later, which
    scanned small repos twice (spending the workspace's AI quota twice).

    Returns how many branches were scheduled as overdue catch-ups, so a caller
    syncing several repos can keep staggering them by passing a running
    `catchup_slot`.
    """
    if _scheduler is None:
        return 0
    # a free scan without an account is one scan: never scheduled (once its
    # visitor signs in, the repo moves to their workspace and syncs again)
    trial = accounts.is_trial_workspace(repo.workspace_id)
    wanted = set() if trial else {_job_id(repo.id, b) for b in repo.branches}
    for job in _scheduler.get_jobs():
        if job.id.startswith(f"{_JOB_PREFIX}{repo.id}::") and job.id not in wanted:
            _scheduler.remove_job(job.id)
    if trial:
        return 0

    now = datetime.now(timezone.utc)
    overdue = 0
    for branch in repo.branches:
        first_run = (now + timedelta(days=repo.interval_days) if branch in scanning_now
                     else _due_at(repo, branch, now))
        is_catchup = first_run is None
        if is_catchup:
            first_run = now + timedelta(
                seconds=_CATCHUP_DELAY + _CATCHUP_STAGGER * (catchup_slot + overdue)
            )
            overdue += 1
        _scheduler.add_job(
            _scan_job,
            "interval",
            days=repo.interval_days,
            # Anchor the first fire to the last completed scan. Without this the
            # interval restarts from now on every sync, so frequent restarts or
            # edits starve the job forever.
            next_run_time=first_run,
            args=[repo.id, branch],
            id=_job_id(repo.id, branch),
            replace_existing=True,
            coalesce=True,      # several missed runs collapse into one
            misfire_grace_time=None,  # run however late; never silently skip
            max_instances=1,
        )
        log.info("scheduled %s @ %s every %sd, next run %s%s",
                 repo.id, branch, repo.interval_days, first_run.isoformat(),
                 " (catch-up: overdue)" if is_catchup else "")
    return overdue


def next_run(repo_id: str, branch: str) -> datetime | None:
    """When this branch's next automatic scan is due, or None if it isn't
    scheduled. Surfaced in the API so the schedule is verifiable in the UI
    instead of only inferable from how stale `last_scan` looks."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(_job_id(repo_id, branch))
    return getattr(job, "next_run_time", None)


def unsync_repo(repo_id: str) -> None:
    if _scheduler is None:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith(f"{_JOB_PREFIX}{repo_id}::"):
            try:
                _scheduler.remove_job(job.id)
            except Exception:  # noqa: BLE001 - job may not exist
                pass


def sync_all() -> None:
    if _scheduler is None:
        return
    # only scan jobs: the daily upkeep job isn't a repo's
    existing = {j.id for j in _scheduler.get_jobs() if j.id.startswith(_JOB_PREFIX)}
    wanted = set()
    slot = 0  # running catch-up counter, so overdue branches spread across repos
    for repo in storage.load_repos():
        slot += sync_repo(repo, catchup_slot=slot)
        wanted.update(_job_id(repo.id, b) for b in repo.branches)
    for stale in existing - wanted:
        try:
            _scheduler.remove_job(stale)
        except Exception:  # noqa: BLE001
            pass


def trigger_now(repo: Repo, branch: str, *, trigger: str = "manual") -> bool:
    """Queue a scan (scan_queue.py) without waiting for it. `trigger` records
    why it ran (manual / initial / upload) on the scan's `scan_runs` row.
    False when that branch was already queued or running."""
    return scan_queue.enqueue(repo, branch, trigger)
