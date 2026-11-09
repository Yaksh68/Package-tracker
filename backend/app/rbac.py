"""Roles and permissions (phase 2b).

`ROUTE_PERMS` says which permission each API route needs, and `authorize`
(a dependency on every API router) enforces it. A route missing from the
table fails closed with a 500, and test_rbac walks the app's routes so such
a route can't ship.

Whose role counts for a route:
- a path with {repo_id}: the caller's role in that repo's workspace;
- a path with {workspace_id}: the caller's role in that workspace;
- anything else: the caller's role in their active workspace (X-Workspace).

A workspace the caller isn't in is a 404, so its existence doesn't leak. A
role without the permission is a 403. Roles are re-read on every request, so
removing someone takes effect immediately. In the local mode the caller is
the local workspace's owner, so everything is allowed.

| Permission  | Viewer | Editor | Admin | Owner | What it covers |
|-------------|:------:|:------:|:-----:|:-----:|----------------|
| view        |   ✓    |   ✓    |   ✓   |   ✓   | repos, results, history, scan activity, CSV, members list |
| edit        |        |   ✓    |   ✓   |   ✓   | add repos (with a write-only token), uploads, release date, ignored packages, schedule |
| scan        |        |   ✓    |   ✓   |   ✓   | run scans (they spend the workspace's AI key) |
| delete_repo |        |        |   ✓   |   ✓   | delete repos and their history |
| settings    |        |        |   ✓   |   ✓   | AI research on/off, provider, model |
| secrets     |        |        |   ✓   |   ✓   | set or remove API keys and git tokens (and see their hints) |
| members     |        |        |   ✓   |   ✓   | invites, removing members, changing roles (never an Owner's) |
| audit       |        |        |   ✓   |   ✓   | read the audit log |
| owner       |        |        |       |   ✓   | manage Owners; rename, transfer or delete the workspace |
"""
from __future__ import annotations

from enum import Enum

from fastapi import Depends, HTTPException, Request

from . import storage
from .auth import Principal, get_principal

ROLES = ("viewer", "editor", "admin", "owner")  # lowest to highest


class Perm(str, Enum):
    VIEW = "view"
    EDIT = "edit"
    SCAN = "scan"
    DELETE_REPO = "delete_repo"
    SETTINGS = "settings"
    SECRETS = "secrets"
    MEMBERS = "members"
    AUDIT = "audit"
    OWNER = "owner"


# Route requirements that aren't workspace permissions.
SIGNED_IN = "signed_in"            # any signed-in user (their own account, their invites)
PLATFORM_ADMIN = "platform_admin"  # PLATFORM_ADMINS; everyone else gets a 404

ROLE_PERMS: dict[str, frozenset[Perm]] = {
    "viewer": frozenset({Perm.VIEW}),
    "editor": frozenset({Perm.VIEW, Perm.EDIT, Perm.SCAN}),
    "admin": frozenset({Perm.VIEW, Perm.EDIT, Perm.SCAN, Perm.DELETE_REPO, Perm.SETTINGS,
                        Perm.SECRETS, Perm.MEMBERS, Perm.AUDIT}),
    "owner": frozenset(Perm),
}


def can(role: str | None, perm: Perm) -> bool:
    return perm in ROLE_PERMS.get(role or "", frozenset())


def rank(role: str | None) -> int:
    return ROLES.index(role) if role in ROLES else -1


def permissions(role: str | None) -> list[str]:
    return sorted(p.value for p in ROLE_PERMS.get(role or "", frozenset()))


def min_role(perm: Perm) -> str:
    return next(r for r in ROLES if perm in ROLE_PERMS[r])


_R = "/api/repos/{repo_id}"
_W = "/api/workspaces/{workspace_id}"
ROUTE_PERMS: dict[tuple[str, str], Perm | str] = {
    # the signed-in account
    ("GET", "/api/me"): SIGNED_IN,
    ("DELETE", "/api/me"): SIGNED_IN,
    # settings and secrets of the active workspace
    ("GET", "/api/settings"): Perm.VIEW,
    ("PUT", "/api/settings"): Perm.SETTINGS,
    ("POST", "/api/settings/ai-test"): Perm.SETTINGS,
    ("GET", "/api/limits"): Perm.VIEW,
    ("PUT", "/api/secrets/{name}"): Perm.SECRETS,
    ("DELETE", "/api/secrets/{name}"): Perm.SECRETS,
    # repos (the active workspace's, or the repo's own)
    ("GET", "/api/repos"): Perm.VIEW,
    ("POST", "/api/repos"): Perm.EDIT,
    ("POST", "/api/repos/upload"): Perm.EDIT,
    ("GET", _R): Perm.VIEW,
    ("PUT", _R): Perm.EDIT,
    ("DELETE", _R): Perm.DELETE_REPO,
    ("POST", f"{_R}/scan"): Perm.SCAN,
    ("POST", f"{_R}/upload"): Perm.EDIT,
    ("GET", f"{_R}/scan-status"): Perm.VIEW,
    ("GET", f"{_R}/scan-log"): Perm.VIEW,
    ("GET", f"{_R}/snapshots"): Perm.VIEW,
    ("GET", f"{_R}/snapshots/{{ts}}/components"): Perm.VIEW,
    ("GET", f"{_R}/snapshots/{{ts}}/report.csv"): Perm.VIEW,
    ("GET", f"{_R}/diff"): Perm.VIEW,
    # workspaces, members, invites, audit
    ("GET", "/api/workspaces"): SIGNED_IN,
    ("POST", "/api/workspaces"): SIGNED_IN,
    ("PUT", _W): Perm.OWNER,
    ("DELETE", _W): Perm.OWNER,
    ("POST", f"{_W}/transfer"): Perm.OWNER,
    ("POST", f"{_W}/leave"): Perm.VIEW,
    ("GET", f"{_W}/members"): Perm.VIEW,
    ("PUT", f"{_W}/members/{{user_id}}"): Perm.MEMBERS,
    ("DELETE", f"{_W}/members/{{user_id}}"): Perm.MEMBERS,
    ("GET", f"{_W}/invites"): Perm.MEMBERS,
    ("POST", f"{_W}/invites"): Perm.MEMBERS,
    ("DELETE", f"{_W}/invites/{{invite_id}}"): Perm.MEMBERS,
    ("GET", f"{_W}/audit"): Perm.AUDIT,
    ("GET", "/api/invites"): SIGNED_IN,
    ("POST", "/api/invites/{invite_id}/accept"): SIGNED_IN,
    ("POST", "/api/invites/{invite_id}/decline"): SIGNED_IN,
    ("POST", "/api/invites/accept-link"): SIGNED_IN,
    # the platform admin
    ("GET", "/api/admin/users"): PLATFORM_ADMIN,
    ("PUT", "/api/admin/users/{user_id}"): PLATFORM_ADMIN,
    ("DELETE", "/api/admin/users/{user_id}"): PLATFORM_ADMIN,
    ("GET", "/api/admin/workspaces"): PLATFORM_ADMIN,
    ("GET", "/api/admin/workspaces/{workspace_id}"): PLATFORM_ADMIN,
    ("GET", "/api/admin/limits"): PLATFORM_ADMIN,
    ("PUT", "/api/admin/limits"): PLATFORM_ADMIN,
    ("GET", "/api/admin/workspaces/{workspace_id}/limits"): PLATFORM_ADMIN,
    ("PUT", "/api/admin/workspaces/{workspace_id}/limits"): PLATFORM_ADMIN,
}


def authorize(request: Request, me: Principal = Depends(get_principal)) -> None:
    """Router dependency: allow the request only if the caller has the
    permission its route needs, in the workspace the route is about."""
    route = request.scope.get("route")
    method = "GET" if request.method == "HEAD" else request.method
    need = ROUTE_PERMS.get((method, getattr(route, "path", "")))
    if need is None:  # fail closed: an undeclared route is a bug, not an open door
        raise HTTPException(500, "this route has no declared permission")
    if need == SIGNED_IN:
        return
    if need == PLATFORM_ADMIN:
        if me.user.platform_role != "admin":
            raise HTTPException(404, "Not Found")
        return

    params = request.path_params
    if "repo_id" in params:
        repo = storage.get_repo(params["repo_id"])
        if repo is None or not me.member_of(repo.workspace_id):
            raise HTTPException(404, f"repo '{params['repo_id']}' not found")
        role = me.role_in(repo.workspace_id)
    elif "workspace_id" in params:
        if not me.member_of(params["workspace_id"]):
            raise HTTPException(404, "workspace not found")
        role = me.role_in(params["workspace_id"])
    else:
        role = me.role
    if not can(role, need):
        raise HTTPException(403, f"this needs the {min_role(need)} role or higher; your role is {role}")
