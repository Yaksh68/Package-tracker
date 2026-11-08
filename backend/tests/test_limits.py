"""Phase 4.2: limits. Platform defaults and per-workspace overrides, each
limit refused with a clear message and counted, the admin endpoints, and no
limits in the local mode."""
import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import accounts, crypto, limits, scheduler, storage
from app.config import get_settings
from app.db import session
from app.db_models import UsageEventRow
from app.main import app
from app.models import Repo

CSRF = {"X-Requested-With": "packages-tracker"}
_ids = iter(range(5000, 90000))


@pytest.fixture
def public(monkeypatch):
    for k, v in {"AUTH_MODE": "github", "PLATFORM_ADMINS": "root"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    crypto.reset()
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch, **kw: True)


def _user(login: str, *, admin: bool = False, workspace: str | None = None):
    user, _ = accounts.sign_in_github_user({"id": next(_ids), "login": login}, f"gho_{login}", admin=admin)
    c = TestClient(app, headers=CSRF)
    c.cookies.set("pt_session", accounts.create_session(user.id))
    if workspace:
        c.headers["X-Workspace"] = workspace
    return c, user


@pytest.fixture
def team(public):
    c, owner = _user("owner")
    ws = c.post("/api/workspaces", json={"name": "Team"}).json()["id"]
    c.headers["X-Workspace"] = ws
    return c, owner, ws


def _hits(name: str) -> int:
    with session() as s:
        rows = s.scalars(select(UsageEventRow).where(UsageEventRow.event == "limit_hit")).all()
        return sum(1 for r in rows if (r.meta or {}).get("limit") == name)


def _repo(c, n: int, **kw):
    return c.post("/api/repos", json={"url": f"https://github.com/t/r{n}", "branches": ["main"], **kw})


# --------------------------------------------------------------------------- #
# values                                                                        #
# --------------------------------------------------------------------------- #
def test_defaults_platform_values_and_overrides_stack(public):
    _, u = _user("someone")
    ws1 = accounts.create_workspace("One", u.id).id
    ws2 = accounts.create_workspace("Two", u.id).id
    assert limits.effective(None) == limits.DEFAULTS
    limits.set_platform({"repos_per_workspace": 20})
    limits.set_overrides(ws1, {"repos_per_workspace": 3, "zip_mb": 10})
    assert limits.effective(ws1)["repos_per_workspace"] == 3
    assert limits.effective(ws2)["repos_per_workspace"] == 20
    limits.set_overrides(ws1, {"repos_per_workspace": None})  # back to the platform's value
    assert limits.effective(ws1)["repos_per_workspace"] == 20 and limits.overrides(ws1) == {"zip_mb": 10}


@pytest.mark.parametrize("bad", [{"repos_per_workspace": 0}, {"repos_per_workspace": "5"},
                                 {"nonsense": 1}, {"repos_per_workspace": True}])
def test_bad_values_are_refused(public, bad):
    with pytest.raises(ValueError):
        limits.set_platform(bad)


def test_the_local_mode_has_no_limits():
    assert limits.value("repos_per_workspace", "local") is None
    c = TestClient(app)
    assert c.get("/api/limits").json() == {"enabled": False}


# --------------------------------------------------------------------------- #
# each limit                                                                    #
# --------------------------------------------------------------------------- #
def test_repos_per_workspace(team):
    c, _, ws = team
    limits.set_overrides(ws, {"repos_per_workspace": 2})
    assert _repo(c, 1).status_code == 201 and _repo(c, 2).status_code == 201
    r = _repo(c, 3)
    assert r.status_code == 409 and "already has 2 repositories" in r.json()["detail"]
    assert r.json()["limit"] == "repos_per_workspace" and _hits("repos_per_workspace") == 1


def test_branches_per_repo_and_the_shortest_interval(team):
    c, _, ws = team
    limits.set_overrides(ws, {"branches_per_repo": 2, "min_interval_days": 7})
    r = c.post("/api/repos", json={"url": "https://github.com/t/b", "branches": ["a", "b", "c"], "interval_days": 7})
    assert r.status_code == 422 and "at most 2 branches" in r.json()["detail"]
    assert _repo(c, 1, interval_days=1).status_code == 422
    rid = _repo(c, 2, interval_days=7).json()["id"]
    assert c.put(f"/api/repos/{rid}", json={"branches": ["a", "b", "c"]}).status_code == 422
    assert c.put(f"/api/repos/{rid}", json={"interval_days": 3}).status_code == 422
    assert c.put(f"/api/repos/{rid}", json={"branches": ["a", "b"], "interval_days": 14}).status_code == 200


def test_manual_scans_per_hour(team):
    c, _, ws = team
    limits.set_overrides(ws, {"manual_scans_per_hour": 2})
    rid = _repo(c, 1).json()["id"]
    for _ in range(2):
        assert c.post(f"/api/repos/{rid}/scan?branch=main").status_code == 202
    r = c.post(f"/api/repos/{rid}/scan?branch=main")
    assert r.status_code == 429 and "2 scans in the last hour" in r.json()["detail"]


def test_team_workspaces_per_user(public):
    c, _ = _user("founder")
    limits.set_platform({"workspaces_per_user": 2})
    assert [c.post("/api/workspaces", json={"name": f"T{i}"}).status_code for i in range(3)] == [201, 201, 409]


def test_members_per_workspace_count_pending_invites_and_hold_at_accept(team):
    c, _, ws = team
    limits.set_overrides(ws, {"members_per_workspace": 2})
    link = c.post(f"/api/workspaces/{ws}/invites", json={"role": "viewer"}).json()  # owner + 1 invite = 2
    r = c.post(f"/api/workspaces/{ws}/invites", json={"role": "viewer"})
    assert r.status_code == 409 and "members and pending invites" in r.json()["detail"]
    # the link still works for one person; a third member can't get in another way
    joiner, _ = _user("joiner")
    assert joiner.post("/api/invites/accept-link", json={"token": link["url"].rsplit("/", 1)[1]}).status_code == 200
    late, late_user = _user("late")
    _, token = accounts.create_invite(ws, "viewer", by="x")  # as if made before the limit came down
    r = late.post("/api/invites/accept-link", json={"token": token})
    assert r.status_code == 409 and "already has 2 members" in r.json()["detail"]
    assert accounts.role_in(ws, late_user.id) is None


def _big_zip(mb: float) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("requirements.txt", "requests==2.31.0\n")
        zf.writestr("blob.bin", os.urandom(int(mb * 1024 * 1024)))
    return buf.getvalue()


def _upload(c, data: bytes, name="App"):
    return c.post("/api/repos/upload", data={"meta": json.dumps({"name": name})},
                  files={"file": ("app.zip", data, "application/zip")})


def test_zip_size_and_upload_storage(team):
    c, _, ws = team
    limits.set_overrides(ws, {"zip_mb": 1})
    r = _upload(c, _big_zip(1.5))
    assert r.status_code == 400 and "larger than 1 MB" in r.json()["detail"]
    limits.set_overrides(ws, {"zip_mb": 5, "upload_storage_mb": 2})
    first = _upload(c, _big_zip(1.2), name="One")
    assert first.status_code == 201
    r = _upload(c, _big_zip(1.2), name="Two")
    assert r.status_code == 413 and "more than 2 MB" in r.json()["detail"]
    # replacing a version counts the new zip instead of the old one
    rid = first.json()["id"]
    r = c.post(f"/api/repos/{rid}/upload", data={"version": "upload"},
               files={"file": ("app.zip", _big_zip(1.3), "application/zip")})
    assert r.status_code == 200, r.text
    assert [rp.id for rp in storage.load_repos(ws)] == [rid]  # "Two" was never created


# --------------------------------------------------------------------------- #
# the API                                                                       #
# --------------------------------------------------------------------------- #
def test_a_workspace_sees_its_limits_and_usage(team):
    c, _, ws = team
    _repo(c, 1)
    body = c.get("/api/limits").json()
    assert body["enabled"] and body["limits"]["repos_per_workspace"] == 10
    assert body["usage"]["repos_per_workspace"] == 1 and body["usage"]["members_per_workspace"] == 1
    assert body["labels"]["zip_mb"] == "Zip size (MB)"


def test_the_platform_admin_sets_defaults_and_overrides(team):
    c, _, ws = team
    root, _ = _user("root", admin=True)
    assert root.put("/api/admin/limits", json={"repos_per_workspace": 25}).json()["limits"]["repos_per_workspace"] == 25
    assert root.put("/api/admin/limits", json={"repos_per_workspace": 0}).status_code == 422
    r = root.put(f"/api/admin/workspaces/{ws}/limits", json={"repos_per_workspace": 40, "zip_mb": None})
    assert r.status_code == 200 and r.json()["effective"]["repos_per_workspace"] == 40
    assert root.put(f"/api/admin/workspaces/{ws}/limits", json={"workspaces_per_user": 9}).status_code == 422
    # the workspace's own admins see that it happened
    assert c.get(f"/api/workspaces/{ws}/audit").json()[0]["action"] == "admin.limits"
    # and nobody else can do it
    assert c.get("/api/admin/limits").status_code == 404
    assert c.put(f"/api/admin/workspaces/{ws}/limits", json={"repos_per_workspace": 99}).status_code == 404


def test_workspace_admins_cant_change_limits_through_settings(team):
    c, _, ws = team
    c.put("/api/settings", json={"limits": {"repos_per_workspace": 999}})
    assert limits.effective(ws)["repos_per_workspace"] == 10
