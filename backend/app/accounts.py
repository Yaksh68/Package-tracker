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
    AuditEventRow,
    InviteRow,
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


def get_workspace(workspace_id: str) -> Workspace | None:
    with session() as s:
        w = s.get(WorkspaceRow, workspace_id)
        return Workspace(id=w.id, name=w.name, personal=w.personal) if w else None


def workspace_creator(workspace_id: str) -> str | None:
    with session() as s:
        w = s.get(WorkspaceRow, workspace_id)
        return w.created_by if w else None


def create_workspace(name: str, owner_id: str) -> Workspace:
    """A team workspace, with its creator as Owner."""
    now = _now()
    with session() as s:
        ws = WorkspaceRow(id=uuid.uuid4().hex, name=name, personal=False, created_by=owner_id,
                          created_at=now)
        s.add(ws)
        s.flush()
        s.add(MembershipRow(workspace_id=ws.id, user_id=owner_id, role="owner", created_at=now))
        return Workspace(id=ws.id, name=ws.name, personal=False, role="owner")


def rename_workspace(workspace_id: str, name: str) -> None:
    with session() as s:
        s.execute(update(WorkspaceRow).where(WorkspaceRow.id == workspace_id).values(name=name))


def workspace_members(workspace_id: str) -> list[dict]:
    with session() as s:
        rows = s.execute(
            select(MembershipRow, UserRow).join(UserRow, UserRow.id == MembershipRow.user_id)
            .where(MembershipRow.workspace_id == workspace_id)
            .order_by(MembershipRow.created_at)
        ).all()
        return [{"user_id": u.id, "login": u.login, "name": u.name, "avatar_url": u.avatar_url,
                 "role": m.role, "joined_at": _utc(m.created_at).isoformat()} for m, u in rows]


def owner_count(workspace_id: str) -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(MembershipRow).where(
            MembershipRow.workspace_id == workspace_id, MembershipRow.role == "owner")) or 0


def set_role(workspace_id: str, user_id: str, role: str) -> None:
    with session() as s:
        s.execute(update(MembershipRow).where(
            MembershipRow.workspace_id == workspace_id, MembershipRow.user_id == user_id,
        ).values(role=role))


def remove_member(workspace_id: str, user_id: str) -> bool:
    with session() as s:
        return bool(s.execute(delete(MembershipRow).where(
            MembershipRow.workspace_id == workspace_id, MembershipRow.user_id == user_id)).rowcount)


def all_users() -> list[dict]:
    """Every signed-up user, for the platform admin (newest first)."""
    with session() as s:
        counts = dict(s.execute(select(MembershipRow.user_id, func.count())
                                .group_by(MembershipRow.user_id)).all())
        rows = s.scalars(select(UserRow).where(UserRow.github_id.is_not(None))
                         .order_by(UserRow.created_at.desc())).all()
        return [{**_user(r).model_dump(mode="json"), "workspaces": counts.get(r.id, 0)} for r in rows]


def all_workspaces() -> list[dict]:
    """Every workspace with its member count, for the platform admin."""
    with session() as s:
        counts = dict(s.execute(select(MembershipRow.workspace_id, func.count())
                                .group_by(MembershipRow.workspace_id)).all())
        rows = s.scalars(select(WorkspaceRow).order_by(WorkspaceRow.created_at.desc())).all()
        return [{"id": w.id, "name": w.name, "personal": w.personal, "created_by": w.created_by,
                 "created_at": _utc(w.created_at).isoformat(), "members": counts.get(w.id, 0)}
                for w in rows]


# --------------------------------------------------------------------------- #
# Invites                                                                       #
# --------------------------------------------------------------------------- #
INVITE_DAYS = 7


class InviteError(ValueError):
    """The invite can't be used; the message is safe to show. `status` is the
    HTTP status: 404 (no such invite for you) or 410 (expired or used)."""

    def __init__(self, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.status = status


def _invite(row: InviteRow, workspace_name: str | None = None) -> dict:
    return {"id": row.id, "workspace_id": row.workspace_id, "workspace_name": workspace_name,
            "role": row.role, "github_login": row.github_login, "link": row.token_hash is not None,
            "created_at": _utc(row.created_at).isoformat(),
            "expires_at": _utc(row.expires_at).isoformat(), "status": row.status}


def create_invite(workspace_id: str, role: str, *, by: str, github_login: str | None = None
                  ) -> tuple[dict, str | None]:
    """A login invite (github_login set) or a one-time link (returns its token,
    which is shown once and stored only as a hash)."""
    now = _now()
    token = None if github_login else _secrets.token_urlsafe(32)
    with session() as s:
        row = InviteRow(id=uuid.uuid4().hex, workspace_id=workspace_id, role=role,
                        github_login=github_login.strip().lower() if github_login else None,
                        token_hash=_hash(token) if token else None, created_by=by,
                        created_at=now, expires_at=now + timedelta(days=INVITE_DAYS),
                        status="pending")
        s.add(row)
        s.flush()
        return _invite(row), token


def _pending(q, now):
    return q.where(InviteRow.status == "pending", InviteRow.expires_at > now)


def workspace_invites(workspace_id: str) -> list[dict]:
    """The workspace's open invites (pending, not expired)."""
    with session() as s:
        rows = s.scalars(_pending(select(InviteRow).where(InviteRow.workspace_id == workspace_id),
                                  _now()).order_by(InviteRow.created_at.desc())).all()
        return [_invite(r) for r in rows]


def invites_for_login(login: str) -> list[dict]:
    """Open invites addressed to this GitHub login, with workspace names."""
    with session() as s:
        rows = s.execute(_pending(
            select(InviteRow, WorkspaceRow.name).join(WorkspaceRow, WorkspaceRow.id == InviteRow.workspace_id)
            .where(InviteRow.github_login == login.lower()), _now())).all()
        return [_invite(r, name) for r, name in rows]


def revoke_invite(workspace_id: str, invite_id: str) -> bool:
    with session() as s:
        return bool(s.execute(update(InviteRow).where(
            InviteRow.id == invite_id, InviteRow.workspace_id == workspace_id,
            InviteRow.status == "pending").values(status="revoked", responded_at=_now())).rowcount)


def _join(s, row: InviteRow, user: User) -> None:
    """Add the user with the invite's role; an existing member keeps the
    higher of the two roles."""
    from .rbac import rank  # (rbac imports auth, which imports this module)
    m = s.get(MembershipRow, (row.workspace_id, user.id))
    if m is None:
        s.add(MembershipRow(workspace_id=row.workspace_id, user_id=user.id, role=row.role,
                            created_at=_now()))
    elif rank(row.role) > rank(m.role):
        m.role = row.role


def respond_to_invite(invite_id: str, user: User, *, accept: bool) -> dict:
    """Accept or decline a login invite addressed to `user`. Returns the invite."""
    now = _now()
    with session() as s:
        row = s.get(InviteRow, invite_id, with_for_update=True)
        if row is None or row.github_login is None or row.github_login != user.login.lower():
            raise InviteError("invite not found")
        if row.status != "pending" or _utc(row.expires_at) <= now:
            raise InviteError("this invite has expired or was already used", 410)
        if accept:
            _join(s, row, user)
        row.status = "accepted" if accept else "declined"
        row.responded_by, row.responded_at = user.id, now
        return _invite(row)


def accept_invite_link(token: str, user: User) -> dict:
    """Use a one-time link: join its workspace with its role. Single use."""
    now = _now()
    with session() as s:
        row = s.scalar(select(InviteRow).where(InviteRow.token_hash == _hash(token))
                       .with_for_update())
        if row is None:
            raise InviteError("invite not found")
        if row.status != "pending" or _utc(row.expires_at) <= now:
            raise InviteError("this invite link has expired or was already used", 410)
        _join(s, row, user)
        row.status = "accepted"
        row.responded_by, row.responded_at = user.id, now
        return _invite(row)


def personal_workspace_ids(user_id: str) -> list[str]:
    with session() as s:
        return list(s.scalars(select(WorkspaceRow.id).where(
            WorkspaceRow.personal, WorkspaceRow.created_by == user_id)))


def sole_member_team_workspace_ids(user_id: str) -> list[str]:
    """Team workspaces this user is the only member of: nobody else could
    reach them once the user is gone."""
    with session() as s:
        sizes = (select(MembershipRow.workspace_id, func.count().label("n"))
                 .group_by(MembershipRow.workspace_id).subquery())
        return list(s.scalars(
            select(WorkspaceRow.id)
            .join(MembershipRow, MembershipRow.workspace_id == WorkspaceRow.id)
            .join(sizes, sizes.c.workspace_id == WorkspaceRow.id)
            .where(MembershipRow.user_id == user_id, WorkspaceRow.personal.is_(False), sizes.c.n == 1)))


def successor(workspace_id: str, leaving_user_id: str) -> dict | None:
    """Who should become Owner when the last Owner goes: the remaining member
    with the highest role, the longest-standing one on a tie."""
    from .rbac import rank  # (rbac imports auth, which imports this module)
    others = [m for m in workspace_members(workspace_id) if m["user_id"] != leaving_user_id]
    # workspace_members is oldest first, and max() keeps the first of equals
    return max(others, key=lambda m: rank(m["role"])) if others else None


def delete_workspace(workspace_id: str) -> None:
    """Delete a workspace's own rows: settings, secrets, memberships and
    invites cascade, plus its analytics and audit rows. The caller deletes its
    repos (and their snapshots and uploads) first."""
    with session() as s:
        s.execute(delete(ScanRunRow).where(ScanRunRow.workspace_id == workspace_id))
        s.execute(delete(UsageEventRow).where(UsageEventRow.workspace_id == workspace_id))
        s.execute(delete(AuditEventRow).where(AuditEventRow.workspace_id == workspace_id))
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
