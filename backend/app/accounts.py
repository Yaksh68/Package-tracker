"""Accounts: users, sessions, workspaces and memberships, per-workspace
settings and encrypted secrets, activity days and usage events.

The storage side of phase 2a. Nothing here knows about HTTP; auth.py and the
API routes call it.
"""
from __future__ import annotations

import hashlib
import logging
import secrets as _secrets
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update

from . import crypto
from .config import get_settings
from .db import is_postgres, session
from .db_models import (
    MembershipRow,
    ScanRunRow,
    SecretRow,
    SessionRow,
    UsageEventRow,
    UserActivityDayRow,
    UserRow,
    WorkspaceRow,
    WorkspaceSettingRow,
)
from .models import LOCAL_USER_ID, LOCAL_WORKSPACE_ID, User, Workspace

log = logging.getLogger("packages-tracker.accounts")

# The workspace secrets the API may set. Their values are never returned.
SECRET_NAMES = ("ai_api_key", "search_api_key", "github_token", "gitlab_token")
_SESSION_TOUCH = timedelta(minutes=5)  # how often last_used_at is refreshed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _insert(model):
    """INSERT that supports ON CONFLICT, on Postgres and on SQLite (tests)."""
    if is_postgres():
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


# --------------------------------------------------------------------------- #
# The built-in local user and workspace                                        #
# --------------------------------------------------------------------------- #
def local_user() -> User:
    return User(id=LOCAL_USER_ID, login="local", name="Local user", platform_role="admin")


def ensure_local_rows() -> None:
    """Make sure the local user and workspace exist. Migration 0008 creates
    them, but a database built straight from the models (the tests) doesn't
    have them. Idempotent, and safe if two callers race."""
    now = _now()
    with session() as s:
        s.execute(_insert(UserRow).values(
            id=LOCAL_USER_ID, login="local", name="Local user", platform_role="admin",
            status="active", created_at=now).on_conflict_do_nothing())
        s.execute(_insert(WorkspaceRow).values(
            id=LOCAL_WORKSPACE_ID, name="Local workspace", personal=True,
            created_by=LOCAL_USER_ID, created_at=now).on_conflict_do_nothing())
        s.execute(_insert(MembershipRow).values(
            workspace_id=LOCAL_WORKSPACE_ID, user_id=LOCAL_USER_ID, role="owner",
            created_at=now).on_conflict_do_nothing())


# --------------------------------------------------------------------------- #
# Users                                                                         #
# --------------------------------------------------------------------------- #
def _user(row: UserRow) -> User:
    return User(id=row.id, github_id=row.github_id, login=row.login, name=row.name,
                avatar_url=row.avatar_url, platform_role=row.platform_role, status=row.status,
                created_at=_utc(row.created_at), last_seen_at=_utc(row.last_seen_at))


def get_user(user_id: str) -> User | None:
    with session() as s:
        row = s.get(UserRow, user_id)
        return _user(row) if row else None


def find_github_user(github_id: int) -> User | None:
    with session() as s:
        row = s.scalar(select(UserRow).where(UserRow.github_id == github_id))
        return _user(row) if row else None


def count_github_users() -> int:
    """Signed-up people (the local user doesn't count toward the cap)."""
    with session() as s:
        return s.scalar(select(func.count()).select_from(UserRow).where(UserRow.github_id.is_not(None))) or 0


def user_github_token(user_id: str) -> str | None:
    with session() as s:
        row = s.get(UserRow, user_id)
        return crypto.decrypt(row.github_token) if row and row.github_token else None


def sign_in_github_user(profile: dict, token: str, *, admin: bool) -> tuple[User, bool]:
    """Create or update the user for a GitHub profile, and store their OAuth
    token encrypted. A new user also gets a personal workspace. Returns
    (user, created)."""
    now = _now()
    login = str(profile["login"])
    with session() as s:
        row = s.scalar(select(UserRow).where(UserRow.github_id == int(profile["id"])))
        created = row is None
        if created:
            row = UserRow(id=uuid.uuid4().hex, github_id=int(profile["id"]), login=login,
                          status="active", created_at=now)
            s.add(row)
            s.flush()
            ws = WorkspaceRow(id=uuid.uuid4().hex, name=f"{login}'s workspace", personal=True,
                              created_by=row.id, created_at=now)
            s.add(ws)
            s.flush()
            s.add(MembershipRow(workspace_id=ws.id, user_id=row.id, role="owner", created_at=now))
        row.login = login
        row.name = profile.get("name") or None
        row.avatar_url = profile.get("avatar_url") or None
        # PLATFORM_ADMINS is the source of truth, so removing a login there
        # removes the rights at the next sign-in
        row.platform_role = "admin" if admin else "user"
        row.github_token = crypto.encrypt(token)
        return _user(row), created


def set_user_status(user_id: str, status: str) -> None:
    with session() as s:
        s.execute(update(UserRow).where(UserRow.id == user_id).values(status=status))


# --------------------------------------------------------------------------- #
# Sessions                                                                      #
# --------------------------------------------------------------------------- #
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: str, user_agent: str | None = None) -> str:
    """Start a session and return the token for the cookie. Only its hash is stored."""
    token = _secrets.token_urlsafe(32)
    now = _now()
    with session() as s:
        s.add(SessionRow(id=_hash(token), user_id=user_id, created_at=now,
                         expires_at=now + timedelta(days=get_settings().session_days),
                         last_used_at=now, user_agent=(user_agent or "")[:300] or None))
    return token


def user_for_session(token: str | None) -> User | None:
    """The active user behind a session token, or None (unknown, expired, or
    the user is disabled)."""
    if not token:
        return None
    now = _now()
    with session() as s:
        row = s.get(SessionRow, _hash(token))
        if row is None:
            return None
        if _utc(row.expires_at) <= now:
            s.delete(row)
            return None
        user = s.get(UserRow, row.user_id)
        if user is None or user.status != "active":
            return None
        if row.last_used_at is None or now - _utc(row.last_used_at) > _SESSION_TOUCH:
            row.last_used_at = now
        return _user(user)


def end_session(token: str | None) -> None:
    if token:
        with session() as s:
            s.execute(delete(SessionRow).where(SessionRow.id == _hash(token)))


def end_user_sessions(user_id: str) -> None:
    with session() as s:
        s.execute(delete(SessionRow).where(SessionRow.user_id == user_id))


# --------------------------------------------------------------------------- #
# Workspaces and memberships                                                    #
# --------------------------------------------------------------------------- #
def user_workspaces(user_id: str) -> list[Workspace]:
    """The user's workspaces with their role in each, personal first."""
    with session() as s:
        rows = s.execute(
            select(WorkspaceRow, MembershipRow.role)
            .join(MembershipRow, MembershipRow.workspace_id == WorkspaceRow.id)
            .where(MembershipRow.user_id == user_id)
            .order_by(WorkspaceRow.personal.desc(), WorkspaceRow.created_at)
        ).all()
        return [Workspace(id=w.id, name=w.name, personal=w.personal, role=role) for w, role in rows]


def role_in(workspace_id: str, user_id: str) -> str | None:
    with session() as s:
        m = s.get(MembershipRow, (workspace_id, user_id))
        return m.role if m else None


def add_member(workspace_id: str, user_id: str, role: str) -> None:
    if workspace_id == LOCAL_WORKSPACE_ID:
        ensure_local_rows()
    with session() as s:
        s.execute(_insert(MembershipRow).values(
            workspace_id=workspace_id, user_id=user_id, role=role, created_at=_now(),
        ).on_conflict_do_nothing())


def personal_workspace_ids(user_id: str) -> list[str]:
    with session() as s:
        return list(s.scalars(select(WorkspaceRow.id).where(
            WorkspaceRow.personal, WorkspaceRow.created_by == user_id)))


def delete_workspace(workspace_id: str) -> None:
    """Delete a workspace's own rows: settings, secrets and memberships
    cascade, plus its analytics rows. The caller deletes its repos (and their
    snapshots and uploads) first."""
    with session() as s:
        s.execute(delete(ScanRunRow).where(ScanRunRow.workspace_id == workspace_id))
        s.execute(delete(UsageEventRow).where(UsageEventRow.workspace_id == workspace_id))
        s.execute(delete(WorkspaceRow).where(WorkspaceRow.id == workspace_id))


def delete_user(user_id: str) -> None:
    """Delete the user and their own analytics rows. Sessions, memberships and
    activity days cascade."""
    with session() as s:
        s.execute(delete(UsageEventRow).where(UsageEventRow.user_id == user_id))
        s.execute(delete(UserRow).where(UserRow.id == user_id))


# --------------------------------------------------------------------------- #
# Per-workspace settings and secrets                                            #
# --------------------------------------------------------------------------- #
def workspace_settings(workspace_id: str) -> dict:
    with session() as s:
        return {r.key: r.value for r in s.scalars(
            select(WorkspaceSettingRow).where(WorkspaceSettingRow.workspace_id == workspace_id))}


def set_workspace_settings(workspace_id: str, values: dict) -> None:
    if workspace_id == LOCAL_WORKSPACE_ID:
        ensure_local_rows()
    with session() as s:
        for k, v in values.items():
            stmt = _insert(WorkspaceSettingRow).values(workspace_id=workspace_id, key=k, value=v)
            s.execute(stmt.on_conflict_do_update(
                index_elements=["workspace_id", "key"], set_={"value": stmt.excluded.value}))


def set_secret(workspace_id: str, name: str, value: str, *, by: str | None = None) -> str:
    """Store a secret encrypted, replacing any earlier value; returns its hint."""
    if name not in SECRET_NAMES:
        raise ValueError(f"unknown secret {name!r}")
    if workspace_id == LOCAL_WORKSPACE_ID:
        ensure_local_rows()
    hint = crypto.hint(value)
    with session() as s:
        stmt = _insert(SecretRow).values(workspace_id=workspace_id, name=name,
                                         ciphertext=crypto.encrypt(value), hint=hint,
                                         updated_at=_now(), updated_by=by)
        s.execute(stmt.on_conflict_do_update(
            index_elements=["workspace_id", "name"],
            set_={"ciphertext": stmt.excluded.ciphertext, "hint": stmt.excluded.hint,
                  "updated_at": stmt.excluded.updated_at, "updated_by": stmt.excluded.updated_by}))
    return hint


def get_secret(workspace_id: str, name: str) -> str | None:
    """The decrypted secret, for server-side use only (never returned by the API)."""
    with session() as s:
        row = s.get(SecretRow, (workspace_id, name))
        return crypto.decrypt(row.ciphertext) if row else None


def delete_secret(workspace_id: str, name: str) -> bool:
    with session() as s:
        return bool(s.execute(delete(SecretRow).where(
            SecretRow.workspace_id == workspace_id, SecretRow.name == name)).rowcount)


def secret_hints(workspace_id: str) -> dict[str, dict]:
    """What the UI may know about each secret: whether it's set, its hint,
    and when it was last changed."""
    with session() as s:
        rows = {r.name: r for r in s.scalars(select(SecretRow).where(SecretRow.workspace_id == workspace_id))}
    return {
        name: ({"set": True, "hint": rows[name].hint,
                "updated_at": _utc(rows[name].updated_at).isoformat()} if name in rows
               else {"set": False, "hint": None, "updated_at": None})
        for name in SECRET_NAMES
    }


# --------------------------------------------------------------------------- #
# Activity and usage events (analytics; never allowed to break a request)       #
# --------------------------------------------------------------------------- #
_active: set[tuple[str, date]] = set()  # in-process: one write per user per day


def touch_activity(user_id: str) -> None:
    today = _now().date()
    if (user_id, today) in _active:
        return
    try:
        with session() as s:
            s.execute(_insert(UserActivityDayRow).values(user_id=user_id, day=today)
                      .on_conflict_do_nothing())
            s.execute(update(UserRow).where(UserRow.id == user_id).values(last_seen_at=_now()))
    except Exception:  # noqa: BLE001
        log.exception("could not record activity for %s", user_id)
        return
    if len(_active) > 10_000:
        _active.clear()
    _active.add((user_id, today))


def record_event(event: str, *, workspace_id: str | None = None, user_id: str | None = None,
                 **meta) -> None:
    try:
        with session() as s:
            s.add(UsageEventRow(at=_now(), event=event, workspace_id=workspace_id,
                                user_id=user_id, meta=meta))
    except Exception:  # noqa: BLE001
        log.exception("could not record usage event %s", event)
