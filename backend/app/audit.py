"""The audit log: who did what in a workspace.

Covers repo changes, scans (including scheduled ones, as "scheduler"),
role, member and invite changes, keys set or removed (the name only, never
the value), AI settings, and a platform admin opening the workspace. Admins
and Owners read it (GET /api/workspaces/{id}/audit, newest first, paged).
Platform-level actions (disabling a user, say) have no workspace.

Writing an audit entry never breaks the action it records: a failure is
logged and dropped. (A stricter system would roll the action back instead;
for this app the action matters more than its log line.)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from .db import session
from .db_models import AuditEventRow, UserRow

if TYPE_CHECKING:
    from .auth import Principal

log = logging.getLogger("packages-tracker.audit")


def record(action: str, *, workspace_id: str | None, actor: "Principal | None" = None,
           actor_kind: str | None = None, target_type: str | None = None,
           target_id: str | None = None, **details) -> None:
    kind = actor_kind or ("user" if actor else "scheduler")
    try:
        with session() as s:
            s.add(AuditEventRow(
                at=datetime.now(timezone.utc), workspace_id=workspace_id,
                actor_user_id=actor.user.id if actor else None, actor_kind=kind,
                action=action, target_type=target_type, target_id=target_id, details=details,
            ))
    except Exception:  # noqa: BLE001
        log.exception("could not record audit event %s", action)


def events(workspace_id: str, *, before_id: int | None = None, limit: int = 50) -> list[dict]:
    """A page of the workspace's audit log, newest first. Pass the last
    entry's id as `before_id` to get the next page."""
    limit = max(1, min(limit, 200))
    with session() as s:
        q = (select(AuditEventRow, UserRow.login)
             .outerjoin(UserRow, UserRow.id == AuditEventRow.actor_user_id)
             .where(AuditEventRow.workspace_id == workspace_id)
             .order_by(AuditEventRow.id.desc()).limit(limit))
        if before_id is not None:
            q = q.where(AuditEventRow.id < before_id)
        rows = s.execute(q).all()
    out = []
    for e, login in rows:
        at = e.at if e.at.tzinfo else e.at.replace(tzinfo=timezone.utc)
        out.append({
            "id": e.id, "at": at.isoformat(), "action": e.action,
            "actor": {"kind": e.actor_kind, "user_id": e.actor_user_id,
                      "login": login or (None if e.actor_user_id is None else "deleted user")},
            "target_type": e.target_type, "target_id": e.target_id, "details": e.details or {},
        })
    return out


def delete_workspace_events(workspace_id: str) -> None:
    with session() as s:
        s.execute(delete(AuditEventRow).where(AuditEventRow.workspace_id == workspace_id))
