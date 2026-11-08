"""Shared by the API modules: deleting a repo, a workspace or a user, along
with everything kept about it."""
from __future__ import annotations

from fastapi import HTTPException

from .. import accounts, scanlog, scheduler, storage, uploads
from ..pipeline import clear_repo_state, is_scanning


def forget_repo(repo_id: str) -> None:
    """Delete a repo and everything kept about it: history, schedule, uploads,
    and in-memory scan state."""
    storage.delete_repo(repo_id)
    scheduler.unsync_repo(repo_id)      # stop every branch's interval job
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


def delete_user_everything(user_id: str) -> None:
    """Delete a user, their personal workspace(s) with everything in them, and
    their analytics rows. Their memberships elsewhere, sessions and activity
    days cascade."""
    personal = accounts.personal_workspace_ids(user_id)
    _refuse_while_scanning([r for ws in personal for r in storage.load_repos(ws)])
    for ws in personal:
        delete_workspace_everything(ws)
    accounts.delete_user(user_id)
