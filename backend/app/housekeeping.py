"""Upkeep (phase 4): history retention, and pausing the scheduled scans of
workspaces nobody uses. The public mode only: the local mode keeps all its
history and never pauses.

- Retention (daily): each branch keeps its newest `snapshots_per_branch`
  scans (a limit, see limits.py). Older ones go, and so do failed scans'
  traces older than the oldest scan kept. The newest scan always stays.
- Inactive workspaces: when no member who isn't disabled has used the service
  for 30 days, the workspace's scheduled scans are skipped. The first time a
  member comes back, they resume (overdue branches catch up at once), and the
  repo list says what happened. A workspace whose members are all disabled
  pauses at once.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import accounts, audit, limits, storage
from .db import session
from .db_models import MembershipRow, UserRow
from .models import LOCAL_WORKSPACE_ID

log = logging.getLogger("packages-tracker.housekeeping")

INACTIVE_DAYS = 30
NOTICE_DAYS = 14  # how long the repo list mentions a pause that's over


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---- retention ------------------------------------------------------------------
def prune_history() -> int:
    """Apply every workspace's `snapshots_per_branch`. Returns how many
    snapshots were deleted."""
    if not limits.enabled():
        return 0
    workspace_of = {r.id: r.workspace_id for r in storage.load_repos()}
    keep_for: dict[str | None, int] = {}
    removed = 0
    for repo_id, branch in storage.snapshot_branches():
        ws = workspace_of.get(repo_id)
        if ws not in keep_for:
            keep_for[ws] = limits.effective(ws)["snapshots_per_branch"]
        removed += storage.prune_snapshots(repo_id, branch, keep_for[ws])
    if removed:
        log.info("retention: deleted %d old snapshot(s)", removed)
    return removed


# ---- inactive workspaces ----------------------------------------------------------
def inactive(workspace_id: str | None) -> bool:
    """No member who isn't disabled has used the service in INACTIVE_DAYS.
    Joining counts as using it, so a new workspace isn't inactive."""
    if not limits.enabled() or not workspace_id or workspace_id == LOCAL_WORKSPACE_ID:
        return False
    cutoff = _now() - timedelta(days=INACTIVE_DAYS)
    with session() as s:
        rows = s.execute(
            select(UserRow.status, UserRow.last_seen_at, UserRow.created_at, MembershipRow.created_at)
            .join(MembershipRow, MembershipRow.user_id == UserRow.id)
            .where(MembershipRow.workspace_id == workspace_id)
        ).all()
    for status, seen, signed_up, joined in rows:
        latest = max(d for d in (_utc(seen), _utc(signed_up), _utc(joined)) if d) if (seen or signed_up or joined) else None
        if status == "active" and latest and latest >= cutoff:
            return False
    return True


def mark_paused(workspace_id: str) -> None:
    """Remember when scheduled scans stopped (once), for the notice."""
    if accounts.workspace_settings(workspace_id).get("scans_paused_at"):
        return
    accounts.set_workspace_settings(workspace_id, {"scans_paused_at": _now().isoformat()})
    audit.record("workspace.scans_paused", workspace_id=workspace_id, actor_kind="scheduler",
                 target_type="workspace", target_id=workspace_id, days_unused=INACTIVE_DAYS)
    log.info("paused the scheduled scans of inactive workspace %s", workspace_id)


def resume_for(user_id: str) -> list[str]:
    """The user is back: resume every workspace of theirs that was paused.
    Returns those workspace ids; the caller reschedules their repos."""
    resumed = []
    for w in accounts.user_workspaces(user_id):
        paused_at = accounts.workspace_settings(w.id).get("scans_paused_at")
        if not paused_at:
            continue
        accounts.set_workspace_settings(w.id, {
            "scans_paused_at": None,
            "scans_resumed": {"paused_at": paused_at, "resumed_at": _now().isoformat()},
        })
        audit.record("workspace.scans_resumed", workspace_id=w.id, actor_kind="user",
                     target_type="workspace", target_id=w.id)
        resumed.append(w.id)
    return resumed


def pause_notice(workspace_id: str) -> dict | None:
    """What the repo list should say: scans are paused, or were until
    recently. None when there's nothing to say."""
    st = accounts.workspace_settings(workspace_id)
    if st.get("scans_paused_at"):
        return {"paused_at": st["scans_paused_at"], "resumed_at": None}
    resumed = st.get("scans_resumed")
    if isinstance(resumed, dict) and resumed.get("resumed_at"):
        try:
            at = datetime.fromisoformat(resumed["resumed_at"])
        except ValueError:
            return None
        if _now() - _utc(at) < timedelta(days=NOTICE_DAYS):
            return {"paused_at": resumed.get("paused_at"), "resumed_at": resumed["resumed_at"]}
    return None
