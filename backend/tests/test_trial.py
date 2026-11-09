"""Phase 7: a free scan without an account. One per browser, limited per
network and per day; the visitor's token reaches that one scan and is never
stored; a guest can only look at their scan; signing in keeps it; unsaved
scans expire. Git and GitHub are faked; no scan really runs."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from app import accounts, analytics, auth, crypto, limits, scan_context, scan_queue, scheduler, storage, trial
from app.agents.clone import CloneError, RemoteInfo
from app.config import get_settings
from app.db import session
from app.db_models import RepoRow, ScanRunRow, UsageEventRow, UserRow
from app.main import app
from app.models import ScanRunRecord

CSRF = {"X-Requested-With": "packages-tracker"}
REPO = "https://github.com/acme/app"


@pytest.fixture
def public(monkeypatch):
    for k, v in {"AUTH_MODE": "github", "GITHUB_OAUTH_CLIENT_ID": "cid", "GITHUB_OAUTH_CLIENT_SECRET": "csecret",
                 "PLATFORM_ADMINS": "TheAdmin", "MAX_USERS": "50"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    crypto.reset()
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch, **kw: None)


@pytest.fixture
def git(monkeypatch):
    """A fake remote with `main` (the default) and `dev`, and a queue that
    only records what it's given."""
    seen = {"remote": [], "queued": []}

    def fake_remote(url, token=None):
        seen["remote"].append((url, token))
        return RemoteInfo(default_branch="main", branches={"main", "dev"})

    monkeypatch.setattr(trial, "remote_info", fake_remote)
    monkeypatch.setattr(scan_queue, "enqueue_trial",
                        lambda repo, branch, token: seen["queued"].append((repo.id, branch, token)) or True)
    return seen


def _visitor() -> TestClient:
    return TestClient(app, headers=CSRF)


def _start(c: TestClient, **body):
    return c.post("/api/trial", json={"url": REPO, **body})


def test_a_visitor_gets_one_free_scan_and_can_only_look_at_it(public, git):
    c = _visitor()
    r = _start(c)
    assert r.status_code == 201, r.text
    repo_id = r.json()["repo_id"]
    assert r.json()["branch"] == "main" and r.json()["hours"] == 24  # the repo's default branch
    assert git["queued"] == [(repo_id, "main", None)]

    me = c.get("/api/me").json()
    assert me["guest"] is True and me["role"] == "viewer"
    assert me["trial"]["repo_id"] == repo_id and me["trial"]["branch"] == "main"
    assert [x["id"] for x in c.get("/api/repos").json()] == [repo_id]
    for path in ("", "/scan-status?branch=main", "/snapshots?branch=main", "/scan-log?branch=main"):
        assert c.get(f"/api/repos/{repo_id}{path}").status_code == 200, path

    # everything else needs signing in, even what a Viewer may do
    for method, path in (("POST", "/api/repos"), ("GET", "/api/settings"), ("POST", "/api/workspaces"),
                         ("PUT", "/api/secrets/ai_api_key"), ("POST", f"/api/repos/{repo_id}/scan?branch=main"),
                         ("GET", f"/api/repos/{repo_id}/snapshots/x/report.csv?branch=main"),
                         ("GET", "/api/workspaces"), ("GET", "/api/invites"), ("DELETE", f"/api/repos/{repo_id}")):
        res = c.request(method, path, json={})
        assert res.status_code == 403 and "Sign in with GitHub" in res.json()["detail"], (method, path)

    again = _start(c, url="https://github.com/acme/other")
    assert again.status_code == 409 and again.json()["repo_id"] == repo_id


def test_the_visitors_token_reaches_that_scan_only_and_is_never_stored(public, git):
    r = _start(_visitor(), branch="dev", token="  ghp_visitors_secret  ")
    assert r.status_code == 201 and r.json()["branch"] == "dev"
    assert git["remote"] == [(REPO, "ghp_visitors_secret")]
    assert git["queued"][0][2] == "ghp_visitors_secret"  # in memory, for that scan
    with session() as s:
        assert s.scalar(select(RepoRow.token_override)) is None
        for table in ("repos", "usage_events", "audit_events", "sessions", "users", "workspace_settings", "secrets"):
            dump = " ".join(str(row) for row in s.execute(text(f"select * from {table}")))
            assert "ghp_visitors_secret" not in dump, table
        event = s.scalar(select(UsageEventRow).where(UsageEventRow.event == "trial_scan"))
        assert event.meta == {"guest": event.meta["guest"], "host": "github.com", "private": True}
        assert event.user_id is None and event.workspace_id is None


def test_a_typo_or_a_missing_branch_doesnt_use_up_the_free_scan(public, git, monkeypatch):
    c = _visitor()
    assert _start(c, url="ftp://example.com/x").status_code == 422
    assert "no branch named 'nope'" in _start(c, branch="nope").json()["detail"]

    def unreachable(url, token=None):
        raise CloneError("repository not found")

    monkeypatch.setattr(trial, "remote_info", unreachable)
    assert "Couldn't read that repository" in _start(c).json()["detail"]
    with session() as s:  # nothing was created, nothing counted
        assert s.scalar(select(func.count()).select_from(UserRow).where(UserRow.status == "guest")) == 0
    monkeypatch.setattr(trial, "remote_info", lambda url, token=None: RemoteInfo("main", {"main"}))
    assert _start(c).status_code == 201


def test_limits_per_network_per_day_and_the_off_switch(public, git):
    limits.set_platform({"trial_scans_per_ip": 2})
    assert _start(_visitor()).status_code == 201
    assert _start(_visitor()).status_code == 201  # another browser, the same network
    third = _start(_visitor())
    assert third.status_code == 429 and "from this network" in third.json()["detail"]

    trial.reset()  # a new day for this network
    limits.set_platform({"trial_scans_per_day": 2})
    used_up = _start(_visitor())
    assert used_up.status_code == 429 and "used up" in used_up.json()["detail"]

    limits.set_platform({"trial_scans_per_day": 0})
    off = _start(_visitor())
    assert off.status_code == 403 and "turned off" in off.json()["detail"]
    with session() as s:
        hits = [m["limit"] for m in s.scalars(select(UsageEventRow.meta).where(UsageEventRow.event == "limit_hit"))]
    assert hits == ["trial_scans_per_ip", "trial_scans_per_day"]


def test_a_busy_queue_turns_visitors_away_without_using_their_scan(public, git, monkeypatch):
    monkeypatch.setattr(scan_queue, "trial_waiting", lambda: trial.QUEUE_MAX)
    r = _start(_visitor())
    assert r.status_code == 503 and "busy" in r.json()["detail"]
    with session() as s:
        assert s.scalar(select(func.count()).select_from(UsageEventRow).where(UsageEventRow.event == "trial_busy")) == 1


def test_signed_in_users_add_repos_the_usual_way(public, git):
    user, _ = accounts.sign_in_github_user({"id": 7, "login": "octo"}, "gho_x", admin=False)
    c = _visitor()
    c.cookies.set("pt_session", accounts.create_session(user.id))
    assert _start(c).status_code == 409


def test_signing_in_keeps_the_free_scan(public, git, monkeypatch, fake_github, oauth_sign_in):
    c = _visitor()
    repo_id = _start(c).json()["repo_id"]
    guest = c.get("/api/me").json()["user"]["id"]
    storage.write_scan_run(ScanRunRecord(repo_id=repo_id, workspace_id=storage.get_repo(repo_id).workspace_id,
                                         branch="main", started_at=datetime.now(timezone.utc), outcome="done",
                                         triggered_by="trial"))
    fake_github(profile={"id": 42, "login": "octo"})
    r = oauth_sign_in(c, next_page=f"/repos/{repo_id}?branch=main")
    assert r.status_code == 302 and r.headers["location"] == f"/repos/{repo_id}?branch=main"

    me = c.get("/api/me").json()
    assert me["guest"] is False and me["user"]["login"] == "octo"
    personal = me["workspaces"][0]["id"]
    repo = storage.get_repo(repo_id)
    assert repo.workspace_id == personal and repo.created_by == me["user"]["id"]
    assert c.get(f"/api/repos/{repo_id}/snapshots/x/report.csv?branch=main").status_code != 403
    assert accounts.get_user(guest) is None  # the guest is gone
    with session() as s:
        assert s.scalar(select(ScanRunRow.workspace_id)) == personal  # its history came along
        saved = s.scalar(select(UsageEventRow).where(UsageEventRow.event == "trial_saved"))
        assert saved.user_id == me["user"]["id"] and saved.meta["repos"] == 1


def test_unsaved_free_scans_go_after_their_hours(public, git):
    c = _visitor()
    repo_id = _start(c).json()["repo_id"]
    ws = storage.get_repo(repo_id).workspace_id
    storage.write_scan_run(ScanRunRecord(repo_id=repo_id, workspace_id=ws, branch="main",
                                         started_at=datetime.now(timezone.utc), outcome="done",
                                         triggered_by="trial"))
    assert trial.expire() == 0  # not yet
    with session() as s:
        s.execute(update(UserRow).where(UserRow.status == "guest")
                  .values(created_at=datetime.now(timezone.utc) - timedelta(hours=25)))
    assert trial.expire() == 1
    assert storage.get_repo(repo_id) is None and c.get("/api/me").status_code == 401
    with session() as s:  # the work it cost still counts, unattached
        assert s.scalar(select(ScanRunRow.workspace_id)) is None
        assert s.scalar(select(func.count()).select_from(UsageEventRow).where(UsageEventRow.event == "trial_scan")) == 1


def test_guests_never_count_as_users_and_their_workspaces_stay_out_of_the_lists(public, git):
    _start(_visitor())
    assert accounts.count_github_users() == 0
    assert accounts.all_users() == []
    assert accounts.all_workspaces() == [w for w in accounts.all_workspaces() if not w["name"] == "Free scan"]
    r = analytics.date_range()
    assert analytics.workspaces(r)["workspaces"] == []
    assert next(t for t in analytics.overview(r)["tiles"] if t["key"] == "users")["value"] == 0


def test_free_scans_are_never_scheduled(public, git, monkeypatch):
    repo_id = _start(_visitor()).json()["repo_id"]
    added = []

    class FakeScheduler:
        def get_jobs(self):
            return []

        def add_job(self, *a, **kw):
            added.append(kw.get("id"))

    monkeypatch.setattr(scheduler, "_scheduler", FakeScheduler())
    assert scheduler.sync_repo(storage.get_repo(repo_id)) == 0 and added == []


def test_a_trial_scan_uses_the_visitors_token_for_its_own_host_only(monkeypatch):
    ctx = scan_context.ScanContext(workspace_id="w", trial=True, trial_token="tok", repo_host="github.com")
    assert ctx.git_token("github.com") == "tok"
    assert ctx.git_token("gitlab.com") is None
    assert ctx.github_api_token() == "tok"
    gitlab = scan_context.ScanContext(workspace_id="w", trial=True, trial_token="tok", repo_host="gitlab.com")
    assert gitlab.github_api_token() is None  # a GitLab token isn't GitHub's
    monkeypatch.setenv("TRIAL_GITHUB_TOKEN", "server-lookup-token")
    get_settings.cache_clear()
    no_token = scan_context.ScanContext(workspace_id="w", trial=True, repo_host="github.com")
    assert no_token.github_api_token() == "server-lookup-token" and no_token.git_token("github.com") is None


def test_the_analytics_count_trials_and_what_became_of_them(public):
    now = datetime.now(timezone.utc)
    with session() as s:
        for event, meta in (("trial_scan", {}), ("trial_scan", {}), ("trial_scan", {}),
                            ("trial_finished", {"outcome": "done"}), ("trial_finished", {"outcome": "error"}),
                            ("trial_saved", {}), ("trial_busy", {}), ("limit_hit", {"limit": "trial_scans_per_ip"}),
                            ("limit_hit", {"limit": "repos_per_workspace"})):
            s.add(UsageEventRow(at=now, event=event, meta=meta))
    out = analytics.trials(analytics.date_range())
    assert out["totals"] == {"started": 3, "results": 1, "failed": 1, "saved": 1, "saved_pct": 33.3}
    assert {r["reason"]: r["count"] for r in out["refused"]} == {"busy": 1, "trial_scans_per_ip": 1}
    assert out["days"][-1] == {"day": now.date().isoformat(), "started": 3, "saved": 1}
    tile = next(t for t in analytics.overview(analytics.date_range())["tiles"] if t["key"] == "trial_scans")
    assert tile["value"] == 3


def test_the_local_mode_has_no_free_scans(git):
    assert _start(_visitor()).status_code == 404
