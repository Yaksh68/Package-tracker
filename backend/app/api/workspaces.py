"""Workspaces, members, invites and the audit log (phase 2b).

The router's `authorize` dependency checks each route's permission from
rbac.ROUTE_PERMS, and a workspace you aren't in is a 404. The rules here are
the ones a permission alone can't express:
- only an Owner can make someone an Owner, or change or remove an Owner;
- the last Owner can't leave, be demoted or be removed;
- a personal workspace belongs to its account alone: it can't take invites,
  be transferred, be left or be deleted (deleting the account deletes it).
  Working with others happens in team workspaces;
- team workspaces, members and invites exist only in the public mode. The
  local mode has a single user.
"""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .. import accounts, audit, rbac
from ..auth import Principal, get_principal
from ..config import get_settings
from .common import delete_workspace_everything

router = APIRouter(prefix="/api", dependencies=[Depends(rbac.authorize)])

Role = Literal["viewer", "editor", "admin", "owner"]
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RoleBody(BaseModel):
    role: Role


class TransferBody(BaseModel):
    user_id: str


class InviteBody(BaseModel):
    role: Role
    # a GitHub login: the invite shows up when that person signs in.
    # Omitted: a one-time link, shown once in the response.
    github_login: str | None = Field(default=None, max_length=39)


class LinkBody(BaseModel):
    token: str = Field(min_length=10, max_length=200)


def _public_only() -> None:
    if not get_settings().public_mode():
        raise HTTPException(404, "there are no team workspaces in the local mode")


def _not_personal(workspace_id: str, message: str) -> None:
    ws = accounts.get_workspace(workspace_id)
    if ws is not None and ws.personal:
        raise HTTPException(400, message)


_PERSONAL = "a personal workspace is just yours: create a team workspace to work with others"


def _audit(me: Principal, action: str, workspace_id: str | None, **kw) -> None:
    audit.record(action, workspace_id=workspace_id, actor=me, **kw)


# --------------------------------------------------------------------------- #
# workspaces                                                                    #
# --------------------------------------------------------------------------- #
@router.get("/workspaces")
def list_workspaces(me: Principal = Depends(get_principal)) -> list[dict]:
    return [w.model_dump() for w in me.workspaces]


@router.post("/workspaces", status_code=201)
def create_workspace(body: NameBody, me: Principal = Depends(get_principal)) -> dict:
    _public_only()
    ws = accounts.create_workspace(body.name.strip(), me.user.id)
    _audit(me, "workspace.create", ws.id, target_type="workspace", target_id=ws.id, name=ws.name)
    accounts.record_event("workspace_created", workspace_id=ws.id, user_id=me.user.id)
    return ws.model_dump()


@router.put("/workspaces/{workspace_id}")
def rename_workspace(workspace_id: str, body: NameBody, me: Principal = Depends(get_principal)) -> dict:
    accounts.rename_workspace(workspace_id, body.name.strip())
    _audit(me, "workspace.rename", workspace_id, target_type="workspace", target_id=workspace_id,
           name=body.name.strip())
    return {"id": workspace_id, "name": body.name.strip()}


@router.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str, me: Principal = Depends(get_principal)) -> Response:
    _public_only()
    ws = accounts.get_workspace(workspace_id)
    if ws is None:
        raise HTTPException(404, "workspace not found")
    if ws.personal:
        raise HTTPException(400, "a personal workspace goes with its account: delete the account instead")
    delete_workspace_everything(workspace_id)
    # the workspace's own log is gone with it; this entry survives at platform level
    _audit(me, "workspace.delete", None, target_type="workspace", target_id=workspace_id, name=ws.name)
    return Response(status_code=204)


@router.post("/workspaces/{workspace_id}/transfer")
def transfer_ownership(workspace_id: str, body: TransferBody,
                       me: Principal = Depends(get_principal)) -> dict:
    """Make another member an Owner and step down to Admin."""
    _public_only()
    _not_personal(workspace_id, _PERSONAL)
    if body.user_id == me.user.id:
        raise HTTPException(400, "you already own this workspace")
    if accounts.role_in(workspace_id, body.user_id) is None:
        raise HTTPException(404, "that person isn't a member of this workspace")
    accounts.set_role(workspace_id, body.user_id, "owner")
    accounts.set_role(workspace_id, me.user.id, "admin")
    _audit(me, "workspace.transfer", workspace_id, target_type="user", target_id=body.user_id)
    return {"workspace_id": workspace_id, "owner": body.user_id}


@router.post("/workspaces/{workspace_id}/leave", status_code=204)
def leave_workspace(workspace_id: str, me: Principal = Depends(get_principal)) -> Response:
    _public_only()
    _not_personal(workspace_id, "you can't leave your personal workspace")
    if me.role_in(workspace_id) == "owner" and accounts.owner_count(workspace_id) == 1:
        raise HTTPException(409, "you're the last Owner: make someone else Owner first")
    accounts.remove_member(workspace_id, me.user.id)
    _audit(me, "member.leave", workspace_id, target_type="user", target_id=me.user.id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# members                                                                       #
# --------------------------------------------------------------------------- #
@router.get("/workspaces/{workspace_id}/members")
def list_members(workspace_id: str, me: Principal = Depends(get_principal)) -> list[dict]:
    if not get_settings().public_mode():
        return [{"user_id": me.user.id, "login": me.user.login, "name": me.user.name,
                 "avatar_url": None, "role": "owner", "joined_at": None}]
    return accounts.workspace_members(workspace_id)


def _guard_owner_change(workspace_id: str, me: Principal, current: str, new: str | None) -> None:
    """Owner rules for changing a member's role (new) or removing them (None)."""
    if (current == "owner" or new == "owner") and me.role_in(workspace_id) != "owner":
        raise HTTPException(403, "only an Owner can make, change or remove an Owner")
    if current == "owner" and new != "owner" and accounts.owner_count(workspace_id) == 1:
        raise HTTPException(409, "that's the last Owner: make someone else Owner first")


@router.put("/workspaces/{workspace_id}/members/{user_id}")
def change_role(workspace_id: str, user_id: str, body: RoleBody,
                me: Principal = Depends(get_principal)) -> dict:
    _public_only()
    current = accounts.role_in(workspace_id, user_id)
    if current is None:
        raise HTTPException(404, "member not found")
    _guard_owner_change(workspace_id, me, current, body.role)
    accounts.set_role(workspace_id, user_id, body.role)
    _audit(me, "member.role_change", workspace_id, target_type="user", target_id=user_id,
           **{"from": current, "to": body.role})
    return {"user_id": user_id, "role": body.role}


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(workspace_id: str, user_id: str, me: Principal = Depends(get_principal)) -> Response:
    _public_only()
    current = accounts.role_in(workspace_id, user_id)
    if current is None:
        raise HTTPException(404, "member not found")
    _guard_owner_change(workspace_id, me, current, None)
    accounts.remove_member(workspace_id, user_id)
    _audit(me, "member.remove", workspace_id, target_type="user", target_id=user_id, role=current)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# invites                                                                       #
# --------------------------------------------------------------------------- #
@router.get("/workspaces/{workspace_id}/invites")
def list_invites(workspace_id: str, me: Principal = Depends(get_principal)) -> list[dict]:
    _public_only()
    return accounts.workspace_invites(workspace_id)


@router.post("/workspaces/{workspace_id}/invites", status_code=201)
def create_invite(workspace_id: str, body: InviteBody, me: Principal = Depends(get_principal)) -> dict:
    _public_only()
    _not_personal(workspace_id, _PERSONAL)
    if body.role == "owner" and me.role_in(workspace_id) != "owner":
        raise HTTPException(403, "only an Owner can invite an Owner")
    login = (body.github_login or "").strip() or None
    if login and not _GITHUB_LOGIN.match(login):
        raise HTTPException(422, "that isn't a valid GitHub username")
    invite, token = accounts.create_invite(workspace_id, body.role, by=me.user.id, github_login=login)
    if token:  # shown this once; only its hash is stored
        invite["url"] = f"{get_settings().public_base_url.rstrip('/')}/invite/{token}"
    _audit(me, "invite.create", workspace_id, target_type="invite", target_id=invite["id"],
           role=body.role, github_login=login or "(link)")
    accounts.record_event("invite_sent", workspace_id=workspace_id, user_id=me.user.id,
                          kind="login" if login else "link")
    return invite


@router.delete("/workspaces/{workspace_id}/invites/{invite_id}", status_code=204)
def revoke_invite(workspace_id: str, invite_id: str, me: Principal = Depends(get_principal)) -> Response:
    _public_only()
    if not accounts.revoke_invite(workspace_id, invite_id):
        raise HTTPException(404, "no open invite with that id")
    _audit(me, "invite.revoke", workspace_id, target_type="invite", target_id=invite_id)
    return Response(status_code=204)


@router.get("/invites")
def my_invites(me: Principal = Depends(get_principal)) -> list[dict]:
    """Open invites addressed to the signed-in user's GitHub login."""
    if not get_settings().public_mode():
        return []
    return accounts.invites_for_login(me.user.login)


def _respond(invite_id: str, me: Principal, accept: bool) -> dict:
    _public_only()
    try:
        invite = accounts.respond_to_invite(invite_id, me.user, accept=accept)
    except accounts.InviteError as e:
        raise HTTPException(e.status, str(e)) from e
    _audit(me, "invite.accept" if accept else "invite.decline", invite["workspace_id"],
           target_type="invite", target_id=invite_id, role=invite["role"])
    if accept:
        accounts.record_event("invite_accepted", workspace_id=invite["workspace_id"], user_id=me.user.id)
    return invite


@router.post("/invites/{invite_id}/accept")
def accept_invite(invite_id: str, me: Principal = Depends(get_principal)) -> dict:
    return _respond(invite_id, me, True)


@router.post("/invites/{invite_id}/decline")
def decline_invite(invite_id: str, me: Principal = Depends(get_principal)) -> dict:
    return _respond(invite_id, me, False)


@router.post("/invites/accept-link")
def accept_invite_link(body: LinkBody, me: Principal = Depends(get_principal)) -> dict:
    _public_only()
    try:
        invite = accounts.accept_invite_link(body.token, me.user)
    except accounts.InviteError as e:
        raise HTTPException(e.status, str(e)) from e
    _audit(me, "invite.accept", invite["workspace_id"], target_type="invite",
           target_id=invite["id"], role=invite["role"], via="link")
    accounts.record_event("invite_accepted", workspace_id=invite["workspace_id"], user_id=me.user.id)
    return invite


# --------------------------------------------------------------------------- #
# audit log                                                                     #
# --------------------------------------------------------------------------- #
@router.get("/workspaces/{workspace_id}/audit")
def audit_log(workspace_id: str, before: int | None = Query(None), limit: int = Query(50, ge=1, le=200),
              me: Principal = Depends(get_principal)) -> list[dict]:
    """Newest first. For the next page, pass the last entry's id as `before`."""
    return audit.events(workspace_id, before_id=before, limit=limit)
