"""ORM tables. Pydantic models in `models.py` stay the API/data contract;
`storage.py` converts between the two, so nothing outside it sees these rows.

`position` columns preserve list order exactly as the scan produced it — the
API and CSV report are ordered, and keys are NOT unique within a snapshot (one
package pinned in two manifests yields two rows with the same key).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .db import JSONBType, JSONType


class Base(DeclarativeBase):
    pass


class RepoRow(Base):
    __tablename__ = "repos"
    __table_args__ = (Index("ix_repos_workspace", "workspace_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # The workspace that owns the repo. Like snapshots, it has no FK:
    # deleting a workspace removes its repos and their history explicitly
    # (accounts.delete_workspace). None = the local workspace (rows written
    # before workspaces existed).
    workspace_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)        # display name
    created_by: Mapped[str | None] = mapped_column(Text)  # user id
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # registry order
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="git", server_default=text("'git'")
    )
    release_date: Mapped[date | None] = mapped_column(Date)
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    inspect_image: Mapped[str | None] = mapped_column(Text)
    image_tags: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    detected_tag_vars: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # encrypted at rest (crypto.encrypt); legacy plain-text rows are
    # re-encrypted at startup
    token_override: Mapped[str | None] = mapped_column(Text)
    ignored_packages: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {branch: iso timestamp}. Kept as a map, not on repo_branches, because it
    # may still hold a branch that has since been removed from the repo.
    last_scan_by_branch: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class RepoBranchRow(Base):
    __tablename__ = "repo_branches"

    repo_id: Mapped[str] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    branch: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class SnapshotRow(Base):
    """One scan of one branch. No FK to `repos`: history may exist for a repo
    id that isn't registered (and `delete_repo` removes it explicitly)."""
    __tablename__ = "snapshots"
    __table_args__ = (
        # also serves (repo_id, branch) history lookups
        UniqueConstraint("repo_id", "branch", "ts", name="uq_snapshots_repo_branch_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. 2026-07-13T10-30-00Z
    # In the snapshot index (history list)? A failed scan still persists its
    # scan log under its ts, but never becomes a listed snapshot.
    indexed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    package_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flag_counts: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The AI research outcome (SnapshotSummary.ai_status / ai_detail)
    ai_status: Mapped[str | None] = mapped_column(String(16))
    ai_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PackageRow(Base):
    __tablename__ = "packages"
    __table_args__ = (
        Index("ix_packages_snapshot", "snapshot_id", "position"),
        # cross-repo lookups: "which branches pin pyjwt 2.13.0?"
        Index("ix_packages_eco_name_version", "ecosystem", text("lower(name)"), "version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str | None] = mapped_column(Text)
    ecosystem: Mapped[str] = mapped_column(String(32), nullable=False)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    is_direct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EnrichmentRow(Base):
    """The whole `Enrichment` lives in `data` (so new model fields need no
    migration); the scalar columns are indexed copies for querying."""
    __tablename__ = "enrichments"
    __table_args__ = (
        Index("ix_enrichments_snapshot", "snapshot_id", "position"),
        Index("ix_enrichments_eco_name_version", "ecosystem", text("lower(name)"), "version"),
        Index("ix_enrichments_verdict", "verdict"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    ecosystem: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    eol_date: Mapped[date | None] = mapped_column(Date)
    data: Mapped[dict] = mapped_column(JSONBType, nullable=False)


class FlagRow(Base):
    __tablename__ = "flags"
    __table_args__ = (
        Index("ix_flags_snapshot", "snapshot_id", "position"),
        Index("ix_flags_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)


class ScanLogRow(Base):
    __tablename__ = "scan_logs"

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    log: Mapped[dict] = mapped_column(JSONType, nullable=False)


class AppSettingRow(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[object] = mapped_column(JSONType, nullable=True)


# --------------------------------------------------------------------------- #
# Accounts and workspaces (phase 2a)                                           #
# --------------------------------------------------------------------------- #
class UserRow(Base):
    """A person who signs in with GitHub, or the built-in "local" user of the
    local mode (no github_id)."""
    __tablename__ = "users"
    __table_args__ = (Index("ux_users_github_id", "github_id", unique=True),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    github_id: Mapped[int | None] = mapped_column(BigInteger)
    login: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    platform_role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")  # user | admin
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active | disabled
    # The GitHub OAuth token from sign-in (encrypted). Scans use it for GitHub
    # API lookups, which gives each user their own 5,000/hour limit.
    github_token: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionRow(Base):
    """A signed-in browser. Only a SHA-256 of the cookie's token is stored, so a
    database leak doesn't hand out live sessions."""
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(Text)


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # every user gets one personal workspace; team workspaces arrive in 2b
    personal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MembershipRow(Base):
    __tablename__ = "memberships"
    __table_args__ = (Index("ix_memberships_user", "user_id"),)

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # owner | admin | editor | viewer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkspaceSettingRow(Base):
    """A workspace's runtime settings: AI research on/off, AI provider and
    model… Secrets are kept apart, in `secrets`."""
    __tablename__ = "workspace_settings"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[object] = mapped_column(JSONType, nullable=True)


class SecretRow(Base):
    """A workspace's API key or access token, encrypted (crypto.encrypt). The
    API can save one but never returns it; it shows only `hint`."""
    __tablename__ = "secrets"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    hint: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(Text)


class UserActivityDayRow(Base):
    """One row per user per day they used the app (daily, weekly and monthly
    active users, and retention)."""
    __tablename__ = "user_activity_days"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)


class UsageEventRow(Base):
    """Feature use, recorded on the server (CSV export, manual scan, …).
    No IP addresses."""
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_at", "at"),
        Index("ix_usage_events_event_at", "event", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event: Mapped[str] = mapped_column(String(48), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class InviteRow(Base):
    """An invitation into a workspace. It's either for a GitHub login (it
    shows up when that person signs in) or a one-time link (only a SHA-256 of
    the link's token is stored). It expires after 7 days."""
    __tablename__ = "invites"
    __table_args__ = (
        Index("ix_invites_workspace", "workspace_id"),
        Index("ix_invites_login", "github_login"),
        Index("ux_invites_token", "token_hash", unique=True),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    github_login: Mapped[str | None] = mapped_column(Text)   # lower-cased
    token_hash: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending | accepted | declined | revoked
    responded_by: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEventRow(Base):
    """Who did what in a workspace: repo changes, scans, role and member
    changes, keys set or removed, AI settings, and a platform admin viewing the
    workspace. Never secret values. Platform-level actions have no workspace."""
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_workspace_id", "workspace_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[str | None] = mapped_column(Text)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # user | scheduler | platform_admin
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(24))
    target_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class ScanRunRow(Base):
    """One row per scan attempt: the fact table behind the admin analytics. It's
    written once, when the scan ends, including failed scans, which never
    become a listed snapshot. Like `snapshots`, it has no FK to `repos`: the
    usage history outlives a deleted repo. Deleting an account (Phase 2a)
    removes its rows by workspace."""
    __tablename__ = "scan_runs"
    __table_args__ = (
        Index("ix_scan_runs_started_at", "started_at"),
        Index("ix_scan_runs_repo_branch", "repo_id", "branch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(Text)  # set from Phase 2a on
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_ts: Mapped[str | None] = mapped_column(String(32))  # None if it never got listed
    triggered_by: Mapped[str] = mapped_column(String(16), nullable=False)  # scheduled | manual | initial | upload
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set once the queue exists
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # done | degraded | error
    stage_seconds: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    issue_stages: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    first_error: Mapped[str | None] = mapped_column(Text)
    package_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    package_counts: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    flag_counts: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    ai_status: Mapped[str | None] = mapped_column(String(16))
    ai_detail: Mapped[str | None] = mapped_column(Text)
    ai_provider: Mapped[str | None] = mapped_column(Text)
    ai_model: Mapped[str | None] = mapped_column(Text)
    ai_batches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_failed_batches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_searches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_cost_usd: Mapped[float | None] = mapped_column(Float)
    upstream_errors: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class SystemSampleRow(Base):
    """An hourly reading of the server's health, for the admin analytics
    (phase 6): the data volume's disk use, the database's size, the app's
    memory and the scan queue. None where this platform can't measure it."""
    __tablename__ = "system_samples"
    __table_args__ = (Index("ix_system_samples_at", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disk_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    db_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uploads_bytes: Mapped[int | None] = mapped_column(BigInteger)
    rss_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mem_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    queue_waiting: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scans_running: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
