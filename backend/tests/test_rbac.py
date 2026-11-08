"""Phase 2b: roles and permissions, membership rules, invites, the audit log
and the platform admin. GitHub is never called; users are created directly."""
import datetime as dt
import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import accounts, crypto, rbac, scheduler, storage
from app.config import get_settings
from app.db import session
from app.db_models import InviteRow
from app.main import app
from app.models import Repo

CSRF = {"X-Requested-With": "packages-tracker"}
PUBLIC_ROUTES = {("GET", "/api/health"), ("GET", "/api/info"), ("GET", "/api/auth/github/login"),
                 ("GET", "/api/auth/github/callback"), ("POST", "/api/auth/logout")}


# --------------------------------------------------------------------------- #
# every route declares a permission, and the check is attached                  #
# --------------------------------------------------------------------------- #
def _walk(routes):
    """Every APIRoute, whether FastAPI keeps included routers flattened into
    app.routes (older versions) or as lazily included routers (0.16x keeps
    them in `_IncludedRouter.original_router`)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes)


def _api_routes():
    for r in _walk(app.routes):
        if r.path.startswith("/api/"):
            for m in r.methods:
                yield m, r.path, r


def test_every_api_route_declares_a_permission():
    declared = set(rbac.ROUTE_PERMS)
    seen = set()
    for method, path, route in _api_routes():
        key = (method, path)
        seen.add(key)
        if key in PUBLIC_ROUTES:
            continue
        assert key in declared, f"{method} {path} has no entry in rbac.ROUTE_PERMS"
        assert any(d.dependency is rbac.authorize for d in route.dependencies), \
            f"{method} {path} isn't behind rbac.authorize"
    assert len(seen) > 40, "the route walk found too few routes"
    assert not declared - seen, f"stale ROUTE_PERMS entries: {declared - seen}"


def test_the_role_table_is_ordered():
    for lower, higher in zip(rbac.ROLES, rbac.ROLES[1:]):
        assert rbac.ROLE_PERMS[lower] < rbac.ROLE_PERMS[higher]


# --------------------------------------------------------------------------- #
# public-mode fixtures                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def public(monkeypatch):
    for k, v in {"AUTH_MODE": "github", "PLATFORM_ADMINS": "root"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    crypto.reset()
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch, **kw: None)


_ids = iter(range(1000, 100000))


def _user(login: str, *, admin: bool = False):
    user, _ = accounts.sign_in_github_user({"id": next(_ids), "login": login}, f"gho_{login}", admin=admin)
    c = TestClient(app, headers=CSRF)
    c.cookies.set("pt_session", accounts.create_session(user.id))
    return c, user


@pytest.fixture
def team(public):
    """A team workspace with one member per role, and a repo in it."""
    owner_c, owner = _user("owner1")
    ws = owner_c.post("/api/workspaces", json={"name": "Team"}).json()["id"]
    clients = {"owner": (owner_c, owner)}
    for role in ("admin", "editor", "viewer"):
        c, u = _user(f"{role}1")
        accounts.add_member(ws, u.id, role)
        c.headers["X-Workspace"] = ws
        clients[role] = (c, u)
    owner_c.headers["X-Workspace"] = ws
    storage.upsert_repo(Repo(id="team-repo", url="https://github.com/t/r", branches=["main"], workspace_id=ws))
    return ws, clients


# --------------------------------------------------------------------------- #
# the role × permission matrix                                                  #
# --------------------------------------------------------------------------- #
ALL = {"viewer", "editor", "admin", "owner"}
EDITORS = {"editor", "admin", "owner"}
ADMINS = {"admin", "owner"}


def _call(c, ws, case):
    method, path, body = case
    path = path.format(ws=ws)
    return getattr(c, method)(path, **({"json": body} if body is not None else {}))


MATRIX = [
    (("get", "/api/repos", None), ALL),
    (("get", "/api/repos/team-repo", None), ALL),
    (("get", "/api/repos/team-repo/snapshots?branch=main", None), ALL),
    (("get", "/api/settings", None), ALL),
    (("get", "/api/workspaces/{ws}/members", None), ALL),
    (("post", "/api/repos", {"url": "https://github.com/t/new", "branches": ["main"]}), EDITORS),
    (("put", "/api/repos/team-repo", {"release_date": "2027-01-01"}), EDITORS),
    (("post", "/api/repos/team-repo/scan?branch=main", None), EDITORS),
    (("put", "/api/settings", {"use_enrichment_agent": True}), ADMINS),
    (("post", "/api/settings/ai-test", {"provider": "claude-cli"}), ADMINS),
    (("put", "/api/secrets/ai_api_key", {"value": "sk-ant-api03-zzzz1234"}), ADMINS),
    (("post", "/api/workspaces/{ws}/invites", {"role": "viewer", "github_login": "someone"}), ADMINS),
    (("get", "/api/workspaces/{ws}/invites", None), ADMINS),
    (("get", "/api/workspaces/{ws}/audit", None), ADMINS),
    (("put", "/api/workspaces/{ws}", {"name": "Renamed"}), {"owner"}),
]


@pytest.mark.parametrize("role", sorted(ALL))
def test_each_role_gets_exactly_its_permissions(team, role):
    ws, clients = team
    c, _ = clients[role]
    for case, allowed in MATRIX:
        status = _call(c, ws, case).status_code
        if role in allowed:
            assert status < 400, (role, case, status)
        else:
            assert status == 403, (role, case, status)


@pytest.mark.parametrize("role,allowed", [("viewer", False), ("editor", False), ("admin", True), ("owner", True)])
def test_deleting_repos_needs_admin(team, role, allowed):
    ws, clients = team
    c, _ = clients[role]
    status = c.delete("/api/repos/team-repo").status_code
    assert status == (204 if allowed else 403)


def test_secret_hints_are_for_admins_only(team):
    ws, clients = team
    clients["owner"][0].put("/api/secrets/ai_api_key", json={"value": "sk-ant-api03-hint9876"})
    assert clients["admin"][0].get("/api/settings").json()["secrets"]["ai_api_key"]["hint"] == "…9876"
    viewer = clients["viewer"][0].get("/api/settings").json()["secrets"]["ai_api_key"]
    assert viewer == {"set": True, "hint": None, "updated_at": None}


def test_outsiders_get_404_never_403(team):
    ws, _ = team
    outsider, _ = _user("outsider")
    for path in (f"/api/workspaces/{ws}/members", f"/api/workspaces/{ws}/audit",
                 "/api/repos/team-repo", "/api/repos/team-repo/snapshots?branch=main"):
        assert outsider.get(path).status_code == 404, path


def test_removing_a_member_takes_effect_on_the_next_request(team):
    ws, clients = team
    editor_c, editor = clients["editor"]
    assert editor_c.get("/api/repos/team-repo").status_code == 200
    assert clients["admin"][0].delete(f"/api/workspaces/{ws}/members/{editor.id}").status_code == 204
    assert editor_c.get("/api/repos/team-repo").status_code == 404


def test_me_reports_the_role_and_permissions(team):
    _, clients = team
    me = clients["editor"][0].get("/api/me").json()
    assert me["role"] == "editor" and me["permissions"] == ["edit", "scan", "view"]
    assert me["role_permissions"]["admin"] == rbac.permissions("admin")


# --------------------------------------------------------------------------- #
# Owner rules                                                                   #
# --------------------------------------------------------------------------- #
def test_only_owners_make_or_change_owners(team):
    ws, clients = team
    admin_c = clients["admin"][0]
    _, owner = clients["owner"]
    _, editor = clients["editor"]
    assert admin_c.put(f"/api/workspaces/{ws}/members/{editor.id}", json={"role": "owner"}).status_code == 403
    assert admin_c.put(f"/api/workspaces/{ws}/members/{owner.id}", json={"role": "admin"}).status_code == 403
    assert admin_c.delete(f"/api/workspaces/{ws}/members/{owner.id}").status_code == 403
    assert admin_c.put(f"/api/workspaces/{ws}/members/{editor.id}", json={"role": "viewer"}).status_code == 200


def test_the_last_owner_cant_leave_or_step_down(team):
    ws, clients = team
    owner_c, owner = clients["owner"]
    _, admin = clients["admin"]
    assert owner_c.post(f"/api/workspaces/{ws}/leave").status_code == 409
    assert owner_c.put(f"/api/workspaces/{ws}/members/{owner.id}", json={"role": "admin"}).status_code == 409
    r = owner_c.post(f"/api/workspaces/{ws}/transfer", json={"user_id": admin.id})
    assert r.status_code == 200
    assert accounts.role_in(ws, admin.id) == "owner" and accounts.role_in(ws, owner.id) == "admin"
    assert owner_c.post(f"/api/workspaces/{ws}/leave").status_code == 204


def test_personal_workspaces_go_with_the_account(public):
    c, user = _user("solo")
    personal = accounts.user_workspaces(user.id)[0].id
    assert c.delete(f"/api/workspaces/{personal}").status_code == 400


def test_an_owner_deletes_a_team_workspace_and_its_repos(team):
    ws, clients = team
    assert clients["owner"][0].delete(f"/api/workspaces/{ws}").status_code == 204
    assert storage.get_repo("team-repo") is None and accounts.get_workspace(ws) is None


def test_the_last_owner_of_a_shared_workspace_cant_delete_their_account(team):
    _, clients = team
    assert clients["owner"][0].delete("/api/me").status_code == 409


def test_a_personal_workspace_stays_personal(public):
    c, user = _user("loner")
    personal = accounts.user_workspaces(user.id)[0].id
    _, friend = _user("friend")
    assert c.post(f"/api/workspaces/{personal}/invites", json={"role": "viewer"}).status_code == 400
    assert c.post(f"/api/workspaces/{personal}/transfer", json={"user_id": friend.id}).status_code == 400
    assert c.post(f"/api/workspaces/{personal}/leave").status_code == 400


def test_deleting_an_account_takes_the_team_workspaces_only_they_were_in(public):
    c, _ = _user("hermit")
    ws = c.post("/api/workspaces", json={"name": "Team of one"}).json()["id"]
    storage.upsert_repo(Repo(id="hermit-repo", url="https://github.com/h/r", branches=["main"], workspace_id=ws))
    assert c.delete("/api/me").status_code == 204
    assert accounts.get_workspace(ws) is None and storage.get_repo("hermit-repo") is None


# --------------------------------------------------------------------------- #
# invites                                                                       #
# --------------------------------------------------------------------------- #
def test_a_login_invite_shows_up_and_can_be_accepted_once(team):
    ws, clients = team
    r = clients["admin"][0].post(f"/api/workspaces/{ws}/invites",
                                 json={"role": "editor", "github_login": "NewBie"})
    assert r.status_code == 201 and "url" not in r.json()
    invite_id = r.json()["id"]
    newbie, user = _user("newbie")
    assert [i["id"] for i in newbie.get("/api/invites").json()] == [invite_id]
    assert newbie.post(f"/api/invites/{invite_id}/accept").status_code == 200
    assert accounts.role_in(ws, user.id) == "editor"
    assert newbie.post(f"/api/invites/{invite_id}/accept").status_code == 410
    assert newbie.get("/api/invites").json() == []


def test_someone_elses_invite_is_a_404(team):
    ws, clients = team
    invite_id = clients["admin"][0].post(f"/api/workspaces/{ws}/invites",
                                         json={"role": "viewer", "github_login": "alice"}).json()["id"]
    mallory, _ = _user("mallory")
    assert mallory.post(f"/api/invites/{invite_id}/accept").status_code == 404


def test_a_link_invite_is_shown_once_and_works_once(team):
    ws, clients = team
    r = clients["owner"][0].post(f"/api/workspaces/{ws}/invites", json={"role": "viewer"})
    token = r.json()["url"].rsplit("/", 1)[1]
    with session() as s:  # only the hash is stored
        assert s.get(InviteRow, r.json()["id"]).token_hash != token
    joiner, user = _user("joiner")
    assert joiner.post("/api/invites/accept-link", json={"token": token}).status_code == 200
    assert accounts.role_in(ws, user.id) == "viewer"
    late, _ = _user("late")
    assert late.post("/api/invites/accept-link", json={"token": token}).status_code == 410


def test_revoked_and_expired_invites_dont_work(team):
    ws, clients = team
    owner_c = clients["owner"][0]
    revoked = owner_c.post(f"/api/workspaces/{ws}/invites", json={"role": "viewer"}).json()
    assert owner_c.delete(f"/api/workspaces/{ws}/invites/{revoked['id']}").status_code == 204
    expired = owner_c.post(f"/api/workspaces/{ws}/invites", json={"role": "viewer"}).json()
    with session() as s:
        s.get(InviteRow, expired["id"]).expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    c, _ = _user("tooslow")
    for inv in (revoked, expired):
        assert c.post("/api/invites/accept-link", json={"token": inv["url"].rsplit("/", 1)[1]}).status_code == 410


def test_only_owners_invite_owners_and_members_keep_a_higher_role(team):
    ws, clients = team
    admin_c, admin = clients["admin"]
    assert admin_c.post(f"/api/workspaces/{ws}/invites",
                        json={"role": "owner", "github_login": "boss"}).status_code == 403
    inv = clients["owner"][0].post(f"/api/workspaces/{ws}/invites",
                                   json={"role": "viewer", "github_login": "admin1"}).json()
    assert admin_c.post(f"/api/invites/{inv['id']}/accept").status_code == 200
    assert accounts.role_in(ws, admin.id) == "admin"  # not downgraded to viewer


# --------------------------------------------------------------------------- #
# audit log                                                                     #
# --------------------------------------------------------------------------- #
def test_actions_are_audited_without_secret_values(team):
    ws, clients = team
    owner_c, owner = clients["owner"]
    _, editor = clients["editor"]
    owner_c.put("/api/repos/team-repo", json={"release_date": "2027-03-01", "token_override": "glpat-secret-1234"})
    owner_c.put("/api/secrets/ai_api_key", json={"value": "sk-ant-api03-audit5555"})
    owner_c.put(f"/api/workspaces/{ws}/members/{editor.id}", json={"role": "viewer"})
    owner_c.post("/api/repos/team-repo/scan?branch=main")

    log = owner_c.get(f"/api/workspaces/{ws}/audit").json()
    actions = [e["action"] for e in log]
    assert actions[:4] == ["scan.manual", "member.role_change", "secret.set", "repo.update"]
    update = next(e for e in log if e["action"] == "repo.update")
    assert update["details"] == {"release_date": "2027-03-01", "token_override": "set"}
    assert update["actor"]["login"] == "owner1"
    text = json.dumps(log)
    assert "glpat-secret" not in text and "sk-ant" not in text


def test_the_audit_log_pages(team):
    ws, clients = team
    owner_c = clients["owner"][0]
    for i in range(5):
        owner_c.put("/api/repos/team-repo", json={"interval_days": i + 1})
    first = owner_c.get(f"/api/workspaces/{ws}/audit?limit=2").json()
    second = owner_c.get(f"/api/workspaces/{ws}/audit?limit=2&before={first[-1]['id']}").json()
    assert len(first) == len(second) == 2 and first[-1]["id"] > second[0]["id"]


# --------------------------------------------------------------------------- #
# the platform admin                                                            #
# --------------------------------------------------------------------------- #
def test_admin_routes_are_invisible_to_everyone_else(public):
    c, _ = _user("nobody")
    for path in ("/api/admin/users", "/api/admin/workspaces"):
        assert c.get(path).status_code == 404


def test_the_platform_admin_manages_users(team):
    root_c, root = _user("root", admin=True)
    victim_c, victim = _user("victim")
    assert any(u["login"] == "victim" for u in root_c.get("/api/admin/users").json())
    assert root_c.put(f"/api/admin/users/{victim.id}", json={"status": "disabled"}).status_code == 200
    assert victim_c.get("/api/me").status_code == 401  # signed out at once
    assert root_c.put(f"/api/admin/users/{root.id}", json={"status": "disabled"}).status_code == 400
    assert root_c.delete(f"/api/admin/users/{victim.id}").status_code == 204
    assert accounts.get_user(victim.id) is None


def test_deleting_the_last_owner_hands_the_workspace_to_the_next_member(team):
    ws, clients = team
    root_c, _ = _user("root", admin=True)
    admin_c, admin = clients["admin"]
    assert root_c.delete(f"/api/admin/users/{clients['owner'][1].id}").status_code == 204
    assert accounts.role_in(ws, admin.id) == "owner" and accounts.owner_count(ws) == 1
    entry = admin_c.get(f"/api/workspaces/{ws}/audit").json()[0]
    assert entry["action"] == "member.role_change" and entry["target_id"] == admin.id
    assert entry["details"]["to"] == "owner" and entry["actor"]["kind"] == "platform_admin"


def test_viewing_a_workspace_is_metadata_only_and_audited(team):
    ws, clients = team
    root_c, _ = _user("root", admin=True)
    detail = root_c.get(f"/api/admin/workspaces/{ws}").json()
    assert [r["id"] for r in detail["repos"]] == ["team-repo"]
    assert {m["role"] for m in detail["members"]} == ALL
    assert "secrets" not in detail and "snapshots" not in json.dumps(detail)
    log = clients["owner"][0].get(f"/api/workspaces/{ws}/audit").json()
    assert log[0]["action"] == "admin.view_workspace" and log[0]["actor"]["kind"] == "platform_admin"


# --------------------------------------------------------------------------- #
# the local mode                                                                #
# --------------------------------------------------------------------------- #
def test_the_local_user_owns_everything_and_is_audited():
    c = TestClient(app)
    me = c.get("/api/me").json()
    assert me["role"] == "owner" and set(me["permissions"]) == {p.value for p in rbac.Perm}
    assert [w["id"] for w in c.get("/api/workspaces").json()] == ["local"]
    assert c.post("/api/workspaces", json={"name": "x"}).status_code == 404
    c.post("/api/repos", json={"url": "https://x/audited", "branches": ["main"]})
    assert c.get("/api/workspaces/local/audit").json()[0]["action"] == "repo.create"
