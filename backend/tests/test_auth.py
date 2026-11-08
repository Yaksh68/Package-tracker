"""Phase 2a, the public mode: GitHub sign-in, sessions, the user cap, CSRF,
workspace isolation, per-workspace settings and secrets, analytics capture
and account deletion. GitHub itself is faked; no network calls."""
import io
import zipfile
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import accounts, auth, crypto, scheduler, storage
from app.config import get_settings
from app.db import session
from app.db_models import UsageEventRow, UserActivityDayRow, UserRow
from app.main import app
from app.models import LOCAL_WORKSPACE_ID, Repo, SnapshotSummary

CSRF = {"X-Requested-With": "packages-tracker"}


@pytest.fixture
def public(monkeypatch):
    """Public mode with GitHub sign-in configured; no real scans."""
    for k, v in {"AUTH_MODE": "github", "GITHUB_OAUTH_CLIENT_ID": "cid",
                 "GITHUB_OAUTH_CLIENT_SECRET": "csecret", "PLATFORM_ADMINS": "TheAdmin",
                 "MAX_USERS": "50"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    crypto.reset()
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch, **kw: None)


def _client() -> TestClient:
    return TestClient(app, headers=CSRF)


def _signed_in(github_id: int, login: str):
    """A client with a live session, skipping the OAuth dance."""
    user, _ = accounts.sign_in_github_user({"id": github_id, "login": login}, f"gho_{login}", admin=False)
    c = _client()
    c.cookies.set("pt_session", accounts.create_session(user.id))
    return c, user


def _fake_github(monkeypatch, *, profile: dict, token: str = "gho_from_github"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            body = parse_qs(request.content.decode())
            assert body["client_secret"] == ["csecret"] and body["code"] == ["the-code"]
            return httpx.Response(200, json={"access_token": token, "token_type": "bearer"})
        if request.url.path == "/user":
            assert request.headers["authorization"] == f"Bearer {token}"
            return httpx.Response(200, json=profile)
        return httpx.Response(404)

    monkeypatch.setattr(auth, "_github_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _oauth_sign_in(c: TestClient, next_page: str | None = None):
    """Run login → callback; returns the callback response."""
    login = c.get("/api/auth/github/login", params={"next": next_page} if next_page else None,
                  follow_redirects=False)
    assert login.status_code == 302
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    return c.get(f"/api/auth/github/callback?code=the-code&state={state}", follow_redirects=False)


# --------------------------------------------------------------------------- #
# sign-in                                                                       #
# --------------------------------------------------------------------------- #
def test_login_redirects_to_github_with_a_state_cookie(public):
    r = _client().get("/api/auth/github/login", follow_redirects=False)
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    q = parse_qs(url.query)
    assert url.netloc == "github.com" and q["client_id"] == ["cid"]
    assert q["redirect_uri"] == ["http://localhost:5173/api/auth/github/callback"]
    assert r.cookies.get(auth.STATE_COOKIE) == q["state"][0]


def test_sign_in_creates_the_user_a_personal_workspace_and_a_session(public, monkeypatch):
    _fake_github(monkeypatch, profile={"id": 42, "login": "octo", "name": "Octo Cat"})
    c = _client()
    r = _oauth_sign_in(c)
    assert r.status_code == 302 and r.headers["location"] == "/"
    me = c.get("/api/me").json()
    assert me["mode"] == "github" and me["user"]["login"] == "octo"
    assert [w["personal"] for w in me["workspaces"]] == [True] and me["role"] == "owner"
    with session() as s:  # the GitHub token is stored encrypted
        assert s.scalar(select(UserRow.github_token).where(UserRow.github_id == 42)).startswith("enc:v1:")


def test_a_wrong_state_is_refused(public, monkeypatch):
    _fake_github(monkeypatch, profile={"id": 1, "login": "x"})
    c = _client()
    c.get("/api/auth/github/login", follow_redirects=False)
    r = c.get("/api/auth/github/callback?code=the-code&state=forged", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login?error=expired"
    assert c.get("/api/me").status_code == 401


def test_cancelling_on_github_returns_to_the_sign_in_page(public):
    r = _client().get("/api/auth/github/callback?error=access_denied", follow_redirects=False)
    assert r.headers["location"] == "/login?error=cancelled"


def test_sign_in_returns_to_the_page_it_started_from(public, monkeypatch):
    _fake_github(monkeypatch, profile={"id": 77, "login": "invitee"})
    r = _oauth_sign_in(_client(), "/invite/tok123?x=1")
    assert r.status_code == 302 and r.headers["location"] == "/invite/tok123?x=1"


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "/api/me",
                                    "/" + chr(92) + "evil.example", "javascript:alert(1)"])
def test_sign_in_never_returns_off_site(public, monkeypatch, target):
    _fake_github(monkeypatch, profile={"id": 78, "login": "careful"})
    assert _oauth_sign_in(_client(), target).headers["location"] == "/"


def test_sign_ups_stop_at_the_cap_but_admins_get_in(public, monkeypatch):
    monkeypatch.setenv("MAX_USERS", "1")
    get_settings.cache_clear()
    _signed_in(1, "first")
    _fake_github(monkeypatch, profile={"id": 2, "login": "second"})
    r = _oauth_sign_in(_client())
    assert r.status_code == 302 and r.headers["location"] == "/login?error=full"
    _fake_github(monkeypatch, profile={"id": 3, "login": "theadmin"})  # case-insensitive match
    c = _client()
    assert _oauth_sign_in(c).status_code == 302
    assert c.get("/api/me").json()["user"]["platform_role"] == "admin"


def test_an_admin_gets_the_repos_from_before_the_public_mode(public, monkeypatch):
    storage.upsert_repo(Repo(id="old", url="https://x/old", branches=["main"]))  # local workspace
    _fake_github(monkeypatch, profile={"id": 9, "login": "TheAdmin"})
    c = _client()
    _oauth_sign_in(c)
    ids = [w["id"] for w in c.get("/api/me").json()["workspaces"]]
    assert LOCAL_WORKSPACE_ID in ids
    assert [r["id"] for r in c.get("/api/repos", headers={"X-Workspace": "local"}).json()] == ["old"]


def test_disabled_users_lose_their_sessions_and_cant_sign_in(public, monkeypatch):
    c, user = _signed_in(5, "gone")
    accounts.set_user_status(user.id, "disabled")
    assert c.get("/api/me").status_code == 401
    _fake_github(monkeypatch, profile={"id": 5, "login": "gone"})
    assert _oauth_sign_in(_client()).headers["location"] == "/login?error=disabled"


def test_logout_ends_the_session(public):
    c, _ = _signed_in(6, "leaver")
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/me").status_code == 401


def test_the_local_mode_has_no_sign_in():
    c = TestClient(app)
    assert c.get("/api/auth/github/login", follow_redirects=False).status_code == 404
    me = c.get("/api/me").json()
    assert me["mode"] == "local" and me["user"]["id"] == "local" and me["workspace_id"] == "local"


# --------------------------------------------------------------------------- #
# CSRF                                                                          #
# --------------------------------------------------------------------------- #
def test_data_changing_requests_need_the_header_and_our_origin(public):
    c, _ = _signed_in(10, "csrf")
    body = {"url": "https://github.com/x/y", "branches": ["main"]}
    no_header = TestClient(app)
    no_header.cookies = c.cookies
    assert no_header.post("/api/repos", json=body).status_code == 403
    assert c.post("/api/repos", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/repos", json=body, headers={"Origin": "http://localhost:5173"}).status_code == 201


def test_unauthenticated_requests_get_401_but_health_is_open(public):
    c = _client()
    assert c.get("/api/repos").status_code == 401
    assert c.get("/api/health").status_code == 200


# --------------------------------------------------------------------------- #
# workspace isolation                                                           #
# --------------------------------------------------------------------------- #
def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("app/requirements.txt", "requests==2.32.3\n")
    return buf.getvalue()


def test_users_never_see_each_others_repos(public):
    alice, _ = _signed_in(20, "alice")
    bob, _ = _signed_in(21, "bob")
    rid = alice.post("/api/repos", json={"url": "https://github.com/a/secret", "branches": ["main"]}).json()["id"]
    assert rid.startswith("r_")  # random IDs in the public mode
    storage.upsert_index_entry(rid, "main", SnapshotSummary(ts="2026-11-08T10-00-00Z"))
    storage.write_packages(rid, "main", "2026-11-08T10-00-00Z", [])

    assert [r["id"] for r in alice.get("/api/repos").json()] == [rid]
    assert bob.get("/api/repos").json() == []
    q = "branch=main"
    for method, path in [
        ("get", f"/api/repos/{rid}"), ("put", f"/api/repos/{rid}"),
        ("post", f"/api/repos/{rid}/scan?{q}"), ("get", f"/api/repos/{rid}/scan-status?{q}"),
        ("get", f"/api/repos/{rid}/scan-log?{q}"), ("get", f"/api/repos/{rid}/snapshots?{q}"),
        ("get", f"/api/repos/{rid}/snapshots/2026-11-08T10-00-00Z/components?{q}"),
        ("get", f"/api/repos/{rid}/snapshots/2026-11-08T10-00-00Z/report.csv?{q}"),
        ("get", f"/api/repos/{rid}/diff?{q}&from=a&to=b"),
        ("delete", f"/api/repos/{rid}"),  # last: alice's delete really deletes it
    ]:
        kwargs = {"json": {"release_date": "2027-01-01"}} if method == "put" else {}
        assert getattr(bob, method)(path, **kwargs).status_code == 404, (method, path)
        assert getattr(alice, method)(path, **kwargs).status_code != 404, (method, path)
    r = bob.post(f"/api/repos/{rid}/upload", files={"file": ("v.zip", _zip(), "application/zip")})
    assert r.status_code == 404


def test_the_same_repo_can_be_added_by_two_users(public):
    alice, _ = _signed_in(30, "alice2")
    bob, _ = _signed_in(31, "bob2")
    body = {"url": "https://github.com/psf/requests", "branches": ["main"]}
    a = alice.post("/api/repos", json=body)
    b = bob.post("/api/repos", json=body)
    assert a.status_code == b.status_code == 201 and a.json()["id"] != b.json()["id"]


def test_choosing_a_workspace_you_are_not_in_is_a_404(public):
    alice, _ = _signed_in(40, "alice3")
    _, bob = _signed_in(41, "bob3")
    bobs = accounts.user_workspaces(bob.id)[0].id
    assert alice.get("/api/repos", headers={"X-Workspace": bobs}).status_code == 404


# --------------------------------------------------------------------------- #
# per-workspace settings and secrets                                            #
# --------------------------------------------------------------------------- #
def test_settings_and_secrets_are_per_workspace_and_write_only(public):
    alice, _ = _signed_in(50, "alice4")
    bob, _ = _signed_in(51, "bob4")
    assert alice.put("/api/settings", json={"ai_provider": "anthropic",
                                            "use_enrichment_agent": True}).status_code == 200
    r = alice.put("/api/secrets/ai_api_key", json={"value": "sk-ant-api03-abcdefgh1234"})
    assert r.json() == {"name": "ai_api_key", "set": True, "hint": "…1234"}

    got = alice.get("/api/settings")
    assert "sk-ant" not in got.text  # never returned
    assert got.json()["ai_provider"] == "anthropic" and got.json()["secrets"]["ai_api_key"]["set"]

    fresh = bob.get("/api/settings").json()  # a new workspace: AI research off, nothing set
    assert fresh["use_enrichment_agent"] is False and fresh["ai_provider"] == ""
    assert not fresh["secrets"]["ai_api_key"]["set"]

    assert alice.delete("/api/secrets/ai_api_key").status_code == 204
    assert not alice.get("/api/settings").json()["secrets"]["ai_api_key"]["set"]


def test_bad_settings_and_unknown_secrets_are_refused(public):
    c, _ = _signed_in(60, "picky")
    assert c.put("/api/settings", json={"ai_provider": "claude-cli"}).status_code == 422
    assert c.put("/api/settings", json={"ai_provider": "skynet"}).status_code == 422
    assert c.put("/api/settings", json={"ai_base_url": "file:///etc"}).status_code == 422
    assert c.put("/api/secrets/root_password", json={"value": "x"}).status_code == 404


# --------------------------------------------------------------------------- #
# analytics capture                                                             #
# --------------------------------------------------------------------------- #
def test_activity_and_feature_use_are_recorded(public):
    c, user = _signed_in(70, "active")
    rid = c.post("/api/repos", json={"url": "https://github.com/a/b", "branches": ["main"]}).json()["id"]
    storage.upsert_index_entry(rid, "main", SnapshotSummary(ts="2026-11-08T10-00-00Z"))
    storage.write_packages(rid, "main", "2026-11-08T10-00-00Z", [])
    c.get(f"/api/repos/{rid}/snapshots/2026-11-08T10-00-00Z/report.csv?branch=main")
    with session() as s:
        assert s.scalar(select(UserActivityDayRow).where(UserActivityDayRow.user_id == user.id))
        events = {e.event for e in s.scalars(select(UsageEventRow).where(UsageEventRow.user_id == user.id))}
        assert s.get(UserRow, user.id).last_seen_at is not None
    assert {"repo_added", "csv_export"} <= events


# --------------------------------------------------------------------------- #
# account deletion                                                              #
# --------------------------------------------------------------------------- #
def test_deleting_an_account_removes_its_workspace_repos_and_data(public):
    alice, user = _signed_in(80, "deleteme")
    bob, _ = _signed_in(81, "staying")
    rid = alice.post("/api/repos", json={"url": "https://github.com/a/b", "branches": ["main"]}).json()["id"]
    bobs = bob.post("/api/repos", json={"url": "https://github.com/c/d", "branches": ["main"]}).json()["id"]
    accounts.set_secret(accounts.user_workspaces(user.id)[0].id, "ai_api_key", "sk-ant-api03-zzzz9999")

    assert alice.delete("/api/me").status_code == 204
    assert alice.get("/api/me").status_code == 401
    assert storage.get_repo(rid) is None and accounts.get_user(user.id) is None
    assert storage.get_repo(bobs) is not None  # nobody else is touched
    with session() as s:
        assert not s.scalars(select(UsageEventRow).where(UsageEventRow.user_id == user.id)).all()


def test_the_local_mode_has_no_account_to_delete():
    assert TestClient(app).delete("/api/me").status_code == 400
