"""Phase 4.4 and 4.5: history retention, and pausing the scheduled scans of
workspaces nobody uses (then resuming them when someone comes back)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app import accounts, audit, crypto, housekeeping, limits, scan_queue, scheduler, storage
from app.config import get_settings
from app.db import session
from app.db_models import MembershipRow, UserRow
from app.main import app
from app.models import Repo, SnapshotSummary

CSRF = {"X-Requested-With": "packages-tracker"}
_ids = iter(range(20000, 90000))


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "github")
    get_settings.cache_clear()
    crypto.reset()


def _ts(day: int) -> str:
    return f"2026-10-{day:02d}T10-00-00Z"


def _history(repo_id: str, listed: list[int], unlisted: list[int] = ()) -> None:
    for d in listed:
        storage.write_packages(repo_id, "main", _ts(d), [])
        storage.upsert_index_entry(repo_id, "main", SnapshotSummary(ts=_ts(d), package_count=0, flag_counts={}))
    for d in unlisted:  # a failed scan keeps its trace, unlisted
        storage.write_packages(repo_id, "main", _ts(d), [])


# --------------------------------------------------------------------------- #
# retention                                                                     #
# --------------------------------------------------------------------------- #
def _all_ts(repo_id: str) -> list[str]:
    """Every snapshot row of the branch, listed or not, newest first."""
    from sqlalchemy import select
    from app.db_models import SnapshotRow
    with session() as s:
        return list(s.scalars(select(SnapshotRow.ts).where(SnapshotRow.repo_id == repo_id)
                              .order_by(SnapshotRow.ts.desc())))


def test_a_branch_keeps_its_newest_scans_and_drops_older_traces():
    storage.upsert_repo(Repo(id="kept", url="https://x/kept", branches=["main"]))
    _history("kept", listed=[1, 3, 5, 7, 9], unlisted=[2, 8])
    assert storage.prune_snapshots("kept", "main", keep=3) == 3   # days 1, 2 and 3 go
    assert [s.ts for s in storage.load_index("kept", "main")] == [_ts(9), _ts(7), _ts(5)]
    assert _all_ts("kept") == [_ts(9), _ts(8), _ts(7), _ts(5)]    # day 8's trace is newer: it stays
    assert storage.prune_snapshots("kept", "main", keep=3) == 0   # nothing more to do
    assert storage.prune_snapshots("kept", "main", keep=0) == 3   # the newest always stays
    assert _all_ts("kept") == [_ts(9)]


def test_retention_follows_each_workspaces_limit(public):
    u, _ = accounts.sign_in_github_user({"id": next(_ids), "login": "keeper"}, "gho_k", admin=False)
    ws_small = accounts.create_workspace("Small", u.id).id
    ws_big = accounts.create_workspace("Big", u.id).id
    limits.set_overrides(ws_small, {"snapshots_per_branch": 2})
    for rid, ws in (("small", ws_small), ("big", ws_big)):
        storage.upsert_repo(Repo(id=rid, url=f"https://x/{rid}", branches=["main"], workspace_id=ws))
        _history(rid, listed=[1, 2, 3, 4])
    assert housekeeping.prune_history() == 2
    assert len(storage.load_index("small", "main")) == 2 and len(storage.load_index("big", "main")) == 4


def test_the_local_mode_keeps_all_history():
    storage.upsert_repo(Repo(id="mine", url="https://x/mine", branches=["main"]))
    _history("mine", listed=list(range(1, 40)))
    assert housekeeping.prune_history() == 0 and len(storage.load_index("mine", "main")) == 39


@pytest.mark.asyncio
async def test_rescheduling_keeps_the_daily_upkeep_job():
    scheduler.start()
    try:
        scheduler.sync_all()
        assert scheduler._scheduler.get_job(scheduler._UPKEEP_JOB) is not None
    finally:
        scheduler.shutdown()


# --------------------------------------------------------------------------- #
# inactive workspaces                                                           #
# --------------------------------------------------------------------------- #
def _member(ws: str, login: str, *, days_ago: int, status: str = "active"):
    user, _ = accounts.sign_in_github_user({"id": next(_ids), "login": login}, f"gho_{login}", admin=False)
    accounts.add_member(ws, user.id, "editor")
    then = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with session() as s:
        s.execute(update(UserRow).where(UserRow.id == user.id)
                  .values(last_seen_at=then, created_at=then, status=status))
        s.execute(update(MembershipRow).where(MembershipRow.user_id == user.id).values(created_at=then))
    return user


@pytest.fixture
def quiet_team(public):
    """A team workspace whose only member was last seen 40 days ago."""
    owner = _member("local", "seed", days_ago=0)  # anyone, to create the workspace
    ws = accounts.create_workspace("Quiet", owner.id).id
    accounts.remove_member(ws, owner.id)
    member = _member(ws, "sleeper", days_ago=40)
    storage.upsert_repo(Repo(id="quiet-repo", url="https://x/q", branches=["main"], workspace_id=ws))
    return ws, member


def test_a_workspace_is_inactive_after_30_quiet_days(public):
    owner = _member("local", "founder", days_ago=0)
    ws = accounts.create_workspace("Busy", owner.id).id
    assert not housekeeping.inactive(ws)
    with session() as s:
        old = datetime.now(timezone.utc) - timedelta(days=31)
        s.execute(update(UserRow).where(UserRow.id == owner.id).values(last_seen_at=old, created_at=old))
        s.execute(update(MembershipRow).where(MembershipRow.user_id == owner.id).values(created_at=old))
    assert housekeeping.inactive(ws)
    _member(ws, "newcomer", days_ago=2)
    assert not housekeeping.inactive(ws)


def test_a_workspace_of_disabled_members_is_inactive_at_once(public):
    owner = _member("local", "banned", days_ago=0, status="disabled")
    ws = accounts.create_workspace("Gone", owner.id).id
    assert housekeeping.inactive(ws)


def test_the_local_workspace_never_pauses():
    assert not housekeeping.inactive("local")


@pytest.mark.asyncio
async def test_a_due_scan_of_an_inactive_workspace_is_skipped_and_recorded(quiet_team, monkeypatch):
    ws, _ = quiet_team
    queued = []
    monkeypatch.setattr(scan_queue, "enqueue", lambda *a, **kw: queued.append(a) or True)
    await scheduler._scan_job("quiet-repo", "main")
    await scheduler._scan_job("quiet-repo", "main")
    assert queued == []
    assert [e["action"] for e in audit.events(ws)] == ["workspace.scans_paused"]  # recorded once
    assert housekeeping.pause_notice(ws)["resumed_at"] is None


def test_a_member_coming_back_resumes_the_scans(quiet_team, monkeypatch):
    ws, member = quiet_team
    housekeeping.mark_paused(ws)
    resynced = []
    monkeypatch.setattr(scheduler, "resync_workspaces", lambda ids: resynced.extend(ids))
    accounts._active.clear()  # their first visit today
    c = TestClient(app, headers={**CSRF, "X-Workspace": ws})
    c.cookies.set("pt_session", accounts.create_session(member.id))
    me = c.get("/api/me").json()
    assert resynced == [ws]
    assert me["scans_paused"]["resumed_at"] is not None and me["scans_paused"]["paused_at"]
    assert not housekeeping.inactive(ws)
    assert audit.events(ws)[0]["action"] == "workspace.scans_resumed"
