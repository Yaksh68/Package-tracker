"""The platform admin (PLATFORM_ADMINS): users and workspaces across the
service. Everyone else gets a 404, so the routes' existence doesn't leak.

The platform admin sees accounts and metadata (who, which workspaces, which
repos), but never scan results or keys unless also a member of that
workspace. Opening a workspace's details is recorded in that workspace's own
audit log, so its admins can see it happened. Limits arrive with phase 4.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from .. import accounts, audit, rbac, storage
from ..auth import Principal, get_principal
from ..models import LOCAL_USER_ID
from .common import delete_user_everything

router = APIRouter(prefix="/api/admin", dependencies=[Depends(rbac.authorize)])


class StatusBody(BaseModel):
    status: Literal["active", "disabled"]


def _target_user(user_id: str, me: Principal):
    if user_id == me.user.id:
        raise HTTPException(400, "you can't change your own account here")
    if user_id == LOCAL_USER_ID:
        raise HTTPException(400, "the built-in local user can't be changed")
    user = accounts.get_user(user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    return user


@router.get("/users")
def list_users(me: Principal = Depends(get_principal)) -> list[dict]:
    return accounts.all_users()


@router.put("/users/{user_id}")
def set_user_status(user_id: str, body: StatusBody, me: Principal = Depends(get_principal)) -> dict:
    user = _target_user(user_id, me)
    accounts.set_user_status(user.id, body.status)
    if body.status == "disabled":
        accounts.end_user_sessions(user.id)  # signed out everywhere, now
    audit.record(f"admin.user_{'disable' if body.status == 'disabled' else 'enable'}",
                 workspace_id=None, actor=me, actor_kind="platform_admin",
                 target_type="user", target_id=user.id, login=user.login)
    return {"id": user.id, "status": body.status}


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: str, me: Principal = Depends(get_principal)) -> Response:
    user = _target_user(user_id, me)
    delete_user_everything(user.id)
    audit.record("admin.user_delete", workspace_id=None, actor=me, actor_kind="platform_admin",
                 target_type="user", target_id=user.id, login=user.login)
    return Response(status_code=204)


@router.get("/workspaces")
def list_workspaces(me: Principal = Depends(get_principal)) -> list[dict]:
    repos = storage.repo_counts_by_workspace()
    return [{**w, "repos": repos.get(w["id"], 0)} for w in accounts.all_workspaces()]


@router.get("/workspaces/{workspace_id}")
def workspace_details(workspace_id: str, me: Principal = Depends(get_principal)) -> dict:
    """Metadata only: members and repos. No scan results, no keys."""
    ws = accounts.get_workspace(workspace_id)
    if ws is None:
        raise HTTPException(404, "workspace not found")
    audit.record("admin.view_workspace", workspace_id=workspace_id, actor=me,
                 actor_kind="platform_admin", target_type="workspace", target_id=workspace_id)
    return {
        **ws.model_dump(exclude={"role"}),
        "members": accounts.workspace_members(workspace_id),
        "repos": [{"id": r.id, "name": r.name, "url": r.url, "source": r.source,
                   "branches": r.branches, "created_at": r.created_at.isoformat() if r.created_at else None,
                   "last_scan_by_branch": {b: t.isoformat() for b, t in r.last_scan_by_branch.items()}}
                  for r in storage.load_repos(workspace_id)],
    }
