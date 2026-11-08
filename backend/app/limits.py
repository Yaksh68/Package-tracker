"""Limits (phase 4): how much one workspace may use on the public service.

- Platform-wide values live in `app_settings` ("limits"), over DEFAULTS; the
  platform admin edits them.
- A workspace can have overrides in `workspace_settings` ("limits"), which
  only the platform admin sets. Workspace admins can't: /api/settings only
  touches its own allowlisted keys.
- The local mode has no limits, beyond the env's upload caps.

A refusal raises LimitError, whose message names the limit, and counts a
`limit_hit` usage event, so the admin can see which limits people run into.
main.py turns LimitError into the HTTP response.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import accounts
from .config import get_settings
from .db import session
from .db_models import AppSettingRow, UsageEventRow, WorkspaceRow
from .models import LOCAL_WORKSPACE_ID

DEFAULTS: dict[str, int] = {
    "workspaces_per_user": 3,
    "members_per_workspace": 10,
    "repos_per_workspace": 10,
    "branches_per_repo": 3,
    "min_interval_days": 1,
    "manual_scans_per_hour": 10,
    "zip_mb": 50,
    "unpacked_mb": 500,
    "upload_storage_mb": 200,
    "snapshots_per_branch": 30,
}

LABELS: dict[str, str] = {
    "workspaces_per_user": "Team workspaces a user can create",
    "members_per_workspace": "Members per workspace (with pending invites)",
    "repos_per_workspace": "Repositories per workspace",
    "branches_per_repo": "Branches or versions per repository",
    "min_interval_days": "Shortest scan interval (days)",
    "manual_scans_per_hour": "Manual scans per workspace per hour",
    "zip_mb": "Zip size (MB)",
    "unpacked_mb": "Unpacked zip size (MB)",
    "upload_storage_mb": "Upload storage per workspace (MB)",
    "snapshots_per_branch": "Scans kept per branch",
}

# limits that apply per user, not per workspace: no workspace overrides
_PLATFORM_ONLY = {"workspaces_per_user"}
_MAX = 100_000

_MESSAGES = {
    "workspaces_per_user": "You've already created {used} team workspaces, the most allowed.",
    "members_per_workspace": "This workspace already has {used} members and pending invites, the most it can have.",
    "repos_per_workspace": "This workspace already has {used} repositories, the most it can have. Delete one first.",
    "branches_per_repo": "A repository can track at most {limit} branches or versions.",
    "min_interval_days": "Scheduled scans can run at most once every {limit} day(s).",
    "manual_scans_per_hour": "This workspace started {used} scans in the last hour, the most allowed. "
                             "Try again later; scheduled scans still run.",
    "upload_storage_mb": "This workspace's uploaded zips would take more than {limit} MB. "
                         "Delete versions you no longer need first.",
}


class LimitError(Exception):
    """A limit refused the request. The message is safe to show."""

    def __init__(self, name: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.name = name
        self.status = status


def enabled() -> bool:
    return get_settings().public_mode()


# ---- values -------------------------------------------------------------------
def _clean(values: dict, *, allow_none: bool) -> dict:
    out = {}
    for k, v in values.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown limit {k!r}")
        if v is None and allow_none:
            out[k] = None
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{k} must be a whole number")
        floor = 1  # 0 would make the feature unusable rather than unlimited
        if not floor <= v <= _MAX:
            raise ValueError(f"{k} must be between {floor} and {_MAX}")
        out[k] = v
    return out


def platform() -> dict[str, int]:
    with session() as s:
        row = s.get(AppSettingRow, "limits")
        stored = row.value if row and isinstance(row.value, dict) else {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def set_platform(values: dict) -> dict[str, int]:
    merged = {**platform(), **_clean(values, allow_none=False)}
    changed = {k: v for k, v in merged.items() if v != DEFAULTS[k]}
    with session() as s:
        row = s.get(AppSettingRow, "limits")
        if row is None:
            s.add(AppSettingRow(key="limits", value=changed))
        else:
            row.value = changed
    return platform()


def overrides(workspace_id: str) -> dict[str, int]:
    stored = accounts.workspace_settings(workspace_id).get("limits")
    return {k: v for k, v in (stored or {}).items() if k in DEFAULTS and k not in _PLATFORM_ONLY}


def set_overrides(workspace_id: str, values: dict) -> dict[str, int]:
    """Set (a number) or clear (None) overrides for one workspace."""
    cleaned = _clean(values, allow_none=True)
    if bad := set(cleaned) & _PLATFORM_ONLY:
        raise ValueError(f"{', '.join(sorted(bad))} applies per user, not per workspace")
    current = overrides(workspace_id)
    for k, v in cleaned.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    accounts.set_workspace_settings(workspace_id, {"limits": current})
    return current


def effective(workspace_id: str | None) -> dict[str, int]:
    values = platform()
    if workspace_id:
        values.update(overrides(workspace_id))
    return values


def value(name: str, workspace_id: str | None) -> int | None:
    """The limit, or None when limits are off (the local mode)."""
    return effective(workspace_id)[name] if enabled() else None


# ---- checks -------------------------------------------------------------------
def _refuse(name: str, *, workspace_id: str | None, user_id: str | None, status: int, **fmt) -> None:
    accounts.record_event("limit_hit", workspace_id=workspace_id, user_id=user_id, limit=name)
    raise LimitError(name, _MESSAGES[name].format(**fmt), status)


def check_count(name: str, *, used: int, workspace_id: str | None, user_id: str | None = None,
                adding: int = 1, status: int = 409) -> None:
    """Refuse when `used + adding` would go past the limit."""
    limit = value(name, workspace_id)
    if limit is not None and used + adding > limit:
        _refuse(name, workspace_id=workspace_id, user_id=user_id, status=status, used=used, limit=limit)


def check_max(name: str, amount: int, *, workspace_id: str | None, user_id: str | None = None) -> None:
    """Refuse an amount above the limit (e.g. branches on one repo)."""
    limit = value(name, workspace_id)
    if limit is not None and amount > limit:
        _refuse(name, workspace_id=workspace_id, user_id=user_id, status=422, used=amount, limit=limit)


def check_min(name: str, amount: int, *, workspace_id: str | None, user_id: str | None = None) -> None:
    """Refuse an amount below the limit (e.g. the scan interval)."""
    limit = value(name, workspace_id)
    if limit is not None and amount < limit:
        _refuse(name, workspace_id=workspace_id, user_id=user_id, status=422, used=amount, limit=limit)


def refuse(name: str, *, workspace_id: str | None, user_id: str | None = None, status: int = 409) -> None:
    """Refuse outright (the caller measured, e.g. upload storage)."""
    _refuse(name, workspace_id=workspace_id, user_id=user_id, status=status, used=None,
            limit=value(name, workspace_id))


# ---- usage --------------------------------------------------------------------
def team_workspaces_created(user_id: str) -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(WorkspaceRow).where(
            WorkspaceRow.created_by == user_id, WorkspaceRow.personal.is_(False))) or 0


def manual_scans_last_hour(workspace_id: str) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    with session() as s:
        return s.scalar(select(func.count()).select_from(UsageEventRow).where(
            UsageEventRow.workspace_id == workspace_id, UsageEventRow.event == "manual_scan",
            UsageEventRow.at >= since)) or 0


def usage(workspace_id: str) -> dict[str, float | int]:
    """What the workspace uses now, for the limits it can run into."""
    from . import storage, uploads  # (storage imports this module's users)
    repos = storage.load_repos(workspace_id)
    members = len(accounts.workspace_members(workspace_id)) if workspace_id != LOCAL_WORKSPACE_ID else 1
    return {
        "repos_per_workspace": len(repos),
        "members_per_workspace": members + len(accounts.workspace_invites(workspace_id)),
        "manual_scans_per_hour": manual_scans_last_hour(workspace_id),
        "upload_storage_mb": round(sum(uploads.stored_bytes(r.id) for r in repos) / 1024 / 1024, 1),
    }
