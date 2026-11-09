"""Shared by the API modules: deleting a repo, a workspace or a user, along
with everything kept about it."""
from __future__ import annotations

from fastapi import HTTPException

from .. import accounts, scan_queue, scanlog, scheduler, storage, uploads
from ..pipeline import clear_repo_state, is_scanning


def forget_repo(repo_id: str) -> None:
    """Delete a repo and everything kept about it: history, schedule, uploads,
    and in-memory scan state."""
    storage.delete_repo(repo_id)
    scheduler.unsync_repo(repo_id)      # stop every branch's interval job
    scan_queue.cancel_repo(repo_id)     # and any scan still waiting for a worker
    uploads.delete(repo_id)             # uploaded zips, if any
    clear_repo_state(repo_id)           # drop in-memory scan status + lock (all branches)
    scanlog.clear_repo(repo_id)         # drop in-memory activity trace (all branches)


def _refuse_while_scanning(repos) -> None:
    if any(is_scanning(r.id, b) for r in repos for b in r.branches):
        raise HTTPException(409, "a scan is in progress; try again once it finishes")


def delete_workspace_everything(workspace_id: str) -> None:
    """Delete a workspace: its repos (history, uploads, schedules) and its own
    rows (settings, secrets, members, invites, analytics, audit)."""
    repos = storage.load_repos(workspace_id)
    _refuse_while_scanning(repos)
    for r in repos:
        forget_repo(r.id)
    accounts.delete_workspace(workspace_id)


def delete_user_everything(user_id: str) -> list[dict]:
    """Delete a user and everything only they could reach: their personal
    workspace and any team workspace they're the only member of, with
    everything in them, plus their analytics rows. Their other memberships,
    sessions and activity days cascade.

    A team workspace this leaves without an Owner gets one: the remaining
    member with the highest role. (`DELETE /api/me` refuses that case first,
    so in practice only a platform admin's deletion gets here.) Returns those
    promotions as {workspace_id, user_id, from} for the caller to audit."""
    gone = accounts.personal_workspace_ids(user_id) + accounts.sole_member_team_workspace_ids(user_id)
    _refuse_while_scanning([r for ws in gone for r in storage.load_repos(ws)])
    promoted = []
    for w in accounts.user_workspaces(user_id):
        if w.id in gone or w.role != "owner" or accounts.owner_count(w.id) > 1:
            continue
        if heir := accounts.successor(w.id, user_id):
            accounts.set_role(w.id, heir["user_id"], "owner")
            promoted.append({"workspace_id": w.id, "user_id": heir["user_id"], "from": heir["role"]})
    for ws in gone:
        delete_workspace_everything(ws)
    accounts.delete_user(user_id)
    return promoted
