"""Free scans without an account (phase 7).

A signed-out visitor can scan one repository: POST /api/trial (api/trial.py)
with its URL, optionally a branch (the repo's default branch otherwise) and a
token for a private repo. They get a guest session, the same cookie as a
signed-in one, that can look at that one scan and nothing else
(rbac.GUEST_ROUTES). Signing in with GitHub moves the repo and its scan into
the person's own workspace (`claim`). Unsaved trials are deleted after
`trial_hours` (`expire`, hourly).

A guest is a user with status "guest", no GitHub id, and a trial workspace of
their own where they're a Viewer. Guests never count as users: not toward the
sign-up cap, not in the admin's lists, not in the analytics' user numbers.

What keeps it cheap and safe:
- One free scan per browser. `trial_scans_per_ip` per network a day, counted
  in memory, so addresses are never stored. `trial_scans_per_day` in all; 0
  turns trials off.
- Free scans have their own queue with one worker (scan_queue.trial), so they
  never hold up signed-in users' scans. At most QUEUE_MAX wait.
- The repo is read with `git ls-remote` first, so a typo or a missing token
  doesn't use up the free scan.
- The visitor's token lives only in memory, for that one scan. It isn't
  stored or logged, and it isn't kept for the saved repo.
- No AI: it runs on each workspace's own key, and a guest has none.

The analytics count trials from usage events with no user or workspace
(trial_scan, trial_finished from the pipeline, trial_busy), so they outlast the
guest; the guest's id is in their details. trial_saved belongs to the person
who signed in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, select, update

from . import accounts, limits, storage
from .agents.clone import CloneError, remote_info
from .db import session
from .db_models import RepoRow, ScanRunRow, UsageEventRow
from .git_rules import RepoUrlError, check_branch, check_repo_url
from .models import Repo, User
from .ratelimit import SlidingWindow

log = logging.getLogger("packages-tracker.trial")

QUEUE_MAX = 5           # free scans waiting for the trial worker; more get "busy, try later"
_DAY = 24 * 3600.0
_per_address = SlidingWindow(window=_DAY)


def reset() -> None:
    """Forget the per-address counts (tests)."""
    global _per_address
    _per_address = SlidingWindow(window=_DAY)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TrialError(Exception):
    """A free scan can't start. The message is safe to show; `extra` goes in
    the response too (e.g. where the visitor's earlier free scan is)."""

    def __init__(self, message: str, status: int = 422, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra


@dataclass
class Started:
    session_token: str
    repo: Repo
    branch: str
    hours: int


def trial_of(user_id: str) -> tuple[Repo, str] | None:
    """A guest's free-scan repo and its branch."""
    for ws in accounts.personal_workspace_ids(user_id):
        for repo in storage.load_repos(ws):
            return repo, repo.branches[0]
    return None


def info(user: User) -> dict | None:
    """For /api/me: a guest's free scan, and when it goes unless they sign in."""
    if user.status != "guest":
        return None
    found = trial_of(user.id)
    created = user.created_at or _now()
    return {
        "repo_id": found[0].id if found else None,
        "branch": found[1] if found else None,
        "expires_at": (created + timedelta(hours=limits.platform()["trial_hours"])).isoformat(),
    }


def started_last_day() -> int:
    since = _now() - timedelta(days=1)
    with session() as s:
        return s.scalar(select(func.count()).select_from(UsageEventRow).where(
            UsageEventRow.event == "trial_scan", UsageEventRow.at >= since)) or 0


def _pick_branch(wanted: str | None, branches: set[str], default: str | None) -> str:
    if wanted:
        if wanted not in branches:
            raise TrialError(f"That repository has no branch named '{wanted}'.")
        return wanted
    for candidate in (default, "main", "master"):
        if candidate and candidate in branches:
            return candidate
    raise TrialError("Couldn't tell which branch to scan. Type the branch's name.")


def start(url: str, branch: str | None, token: str | None, *, address: str,
          current_user: User | None, user_agent: str | None) -> Started:
    """Check everything, then create the guest, their workspace, the repo and
    a session. The caller queues the scan (on the event loop) with the token.
    Raises TrialError, or limits.LimitError for the daily limits. Blocking:
    run it in a worker thread."""
    lim = limits.platform()
    if lim["trial_scans_per_day"] == 0:
        raise TrialError("Free scans without an account are turned off. Sign in with GitHub to scan.", 403)
    if current_user is not None:
        if current_user.status == "guest":
            found = trial_of(current_user.id)
            raise TrialError("You've used your free scan. Sign in with GitHub to keep it and to scan more.", 409,
                             repo_id=found[0].id if found else None, branch=found[1] if found else None)
        raise TrialError("You're signed in: add the repository from your workspace instead.", 409)
    try:
        url = check_repo_url(url)
        branch = check_branch(branch) if branch and branch.strip() else None
    except RepoUrlError as e:
        raise TrialError(str(e)) from e
    from . import scan_queue  # (it imports the pipeline)
    if scan_queue.trial_waiting() >= QUEUE_MAX:
        accounts.record_event("trial_busy")
        raise TrialError("Free scans are busy right now. Try again in a few minutes, "
                         "or sign in with GitHub to scan straight away.", 503)
    try:
        remote = remote_info(url, token)
    except CloneError:
        raise TrialError("Couldn't read that repository. Check the address. For a private "
                         "repository, add a token that can read it.") from None
    branch = _pick_branch(branch, remote.branches, remote.default_branch)
    # the limits last, so a refused request (a typo, a busy queue) doesn't count
    if started_last_day() >= lim["trial_scans_per_day"]:
        limits.refuse("trial_scans_per_day", workspace_id=None, status=429)
    if _per_address.hit(address, lim["trial_scans_per_ip"]):
        limits.refuse("trial_scans_per_ip", workspace_id=None, status=429)

    guest, workspace_id = accounts.create_guest()
    repo = Repo(id=storage.new_repo_id(), workspace_id=workspace_id, name=storage.slugify_repo_id(url),
                created_by=guest.id, url=url, branches=[branch], created_at=_now())
    storage.upsert_repo(repo)
    hours = lim["trial_hours"]
    session_token = accounts.create_session(guest.id, user_agent, hours=hours)
    accounts.record_event("trial_scan", guest=guest.id, host=urlsplit(url).hostname, private=bool(token))
    return Started(session_token, repo, branch, hours)


def claim(guest_id: str, user_id: str) -> list[str]:
    """Signing in after a free scan: move the guest's repo, with its scan
    history, into the person's personal workspace, schedule it like any repo,
    and delete the guest. Returns the repo ids moved."""
    from . import scheduler
    target = next(iter(accounts.personal_workspace_ids(user_id)), None)
    trial_ws = accounts.personal_workspace_ids(guest_id)
    moved = [r.id for ws in trial_ws for r in storage.load_repos(ws)]
    if target and moved:
        with session() as s:
            s.execute(update(RepoRow).where(RepoRow.id.in_(moved))
                      .values(workspace_id=target, created_by=user_id))
            s.execute(update(ScanRunRow).where(ScanRunRow.repo_id.in_(moved)).values(workspace_id=target))
    _delete_guest(guest_id)
    for repo_id in moved:
        if repo := storage.get_repo(repo_id):
            scheduler.sync_repo(repo)
    if moved:
        accounts.record_event("trial_saved", workspace_id=target, user_id=user_id, guest=guest_id,
                              repos=len(moved))
    return moved


def _delete_guest(guest_id: str) -> None:
    from .api.common import delete_user_everything
    # the trial's scan_runs stay, unattached, so the analytics still count the work
    with session() as s:
        s.execute(update(ScanRunRow).where(
            ScanRunRow.workspace_id.in_(accounts.personal_workspace_ids(guest_id))).values(workspace_id=None))
    delete_user_everything(guest_id)


def expire() -> int:
    """Hourly (scheduler.py): delete the free scans nobody saved within
    `trial_hours`. Returns how many went."""
    cutoff = _now() - timedelta(hours=limits.platform()["trial_hours"])
    gone = 0
    for guest_id in accounts.guests_created_before(cutoff):
        try:
            _delete_guest(guest_id)
            gone += 1
        except Exception:  # noqa: BLE001 - e.g. its scan is still running: next hour
            log.exception("could not delete the expired free scan of guest %s", guest_id)
    if gone:
        log.info("deleted %d expired free scan(s)", gone)
    return gone
