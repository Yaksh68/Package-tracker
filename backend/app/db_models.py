"""ORM tables. Pydantic models in `models.py` stay the API/data contract;
`storage.py` converts between the two, so nothing outside it sees these rows.

`position` columns preserve list order exactly as the scan produced it — the
API and CSV report are ordered, and keys are NOT unique within a snapshot (one
package pinned in two manifests yields two rows with the same key).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
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

    id: Mapped[str] = mapped_column(Text, primary_key=True)
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
    token_override: Mapped[str | None] = mapped_column(Text)  # plain text, as repos.json was
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
