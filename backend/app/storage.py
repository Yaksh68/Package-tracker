"""Storage — repos, runtime settings and snapshot history in the database.

Every function keeps the signature and Pydantic return types of the old
file-based version, so routes, pipeline and scheduler don't know or care where
data lives. Each call is one transaction (`db.session()`).

Snapshot history is per (repo, branch): each scan is a `snapshots` row keyed by
its timestamp, with packages / enrichment / flags / scan-log child rows.
"""
from __future__ import annotations

import csv
import io
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.orm import Session

from . import crypto, flags
from .db import session
from .db_models import (
    EnrichmentRow,
    FlagRow,
    PackageRow,
    RepoBranchRow,
    RepoRow,
    ScanLogRow,
    ScanRunRow,
    SnapshotRow,
)
from .models import (
    LOCAL_WORKSPACE_ID,
    Enrichment,
    Flag,
    Package,
    Repo,
    ScanRunRecord,
    SnapshotSummary,
)


# --------------------------------------------------------------------------- #
# Naming helpers                                                               #
# --------------------------------------------------------------------------- #
def branch_dir_name(branch: str) -> str:
    """Filename-safe slug for a branch name (branches may contain `/`). Used for
    CSV download names and to read the legacy file layout."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", branch.strip())
    return slug or "branch"


def new_snapshot_ts(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H-%M-%SZ")


def slugify_repo_id(url: str) -> str:
    name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")
    return slug or "repo"


def new_repo_id() -> str:
    """A random repo ID, for the public mode. A slug of the URL would collide
    as soon as two users add the same repo."""
    return f"r_{secrets.token_hex(5)}"


# --------------------------------------------------------------------------- #
# Repo registry                                                                #
# --------------------------------------------------------------------------- #
def _utc(dt: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes; everything here is stored as UTC.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _repo_from_row(row: RepoRow, branches: list[str]) -> Repo:
    return Repo(
        id=row.id,
        workspace_id=row.workspace_id or LOCAL_WORKSPACE_ID,
        name=row.name,
        created_by=row.created_by,
        url=row.url,
        source=row.source,
        branches=branches,
        release_date=row.release_date,
        interval_days=row.interval_days,
        inspect_image=row.inspect_image,
        image_tags=row.image_tags or {},
        detected_tag_vars=row.detected_tag_vars or [],
        token_override=crypto.decrypt(row.token_override) if row.token_override else None,
        ignored_packages=row.ignored_packages or [],
        created_at=_utc(row.created_at),
        last_scan_by_branch=row.last_scan_by_branch or {},
    )


def _branches_by_repo(s: Session, repo_ids: list[str] | None = None) -> dict[str, list[str]]:
    q = select(RepoBranchRow).order_by(RepoBranchRow.repo_id, RepoBranchRow.position)
    if repo_ids is not None:
        q = q.where(RepoBranchRow.repo_id.in_(repo_ids))
    out: dict[str, list[str]] = {}
    for b in s.scalars(q):
        out.setdefault(b.repo_id, []).append(b.branch)
    return out


def _in_workspace(workspace_id: str):
    cond = RepoRow.workspace_id == workspace_id
    if workspace_id == LOCAL_WORKSPACE_ID:  # rows from before workspaces existed
        cond = or_(cond, RepoRow.workspace_id.is_(None))
    return cond


def load_repos(workspace_id: str | None = None) -> list[Repo]:
    """Every repo (the scheduler), or one workspace's (the API)."""
    with session() as s:
        q = select(RepoRow).order_by(RepoRow.position)
        if workspace_id is not None:
            q = q.where(_in_workspace(workspace_id))
        rows = s.scalars(q).all()
        branches = _branches_by_repo(s, [r.id for r in rows] if workspace_id is not None else None)
        return [_repo_from_row(r, branches.get(r.id, [])) for r in rows]


def encrypt_plaintext_tokens() -> int:
    """Encrypt any per-repo token still stored in plain text (rows written
    before encryption at rest). Run at startup; returns how many changed."""
    with session() as s:
        rows = s.scalars(select(RepoRow).where(RepoRow.token_override.is_not(None))).all()
        changed = 0
        for row in rows:
            if row.token_override and not crypto.is_encrypted(row.token_override):
                row.token_override = crypto.encrypt(row.token_override)
                changed += 1
        return changed


def get_repo(repo_id: str) -> Repo | None:
    with session() as s:
        row = s.get(RepoRow, repo_id)
        if row is None:
            return None
        return _repo_from_row(row, _branches_by_repo(s, [repo_id]).get(repo_id, []))


def _write_repo(s: Session, repo: Repo) -> None:
    # Lock the repo row so overlapping writers (a PUT racing the pipeline's
    # last-scan update) run one at a time; otherwise both delete-then-insert the
    # branch rows and the loser hits a duplicate key. SQLite ignores FOR UPDATE
    # and serialises writers anyway.
    row = s.get(RepoRow, repo.id, with_for_update=True)
    if row is None:
        pos = (s.scalar(select(func.max(RepoRow.position))) or 0) + 1
        row = RepoRow(id=repo.id, position=pos)
        s.add(row)
    dumped = repo.model_dump(mode="json")
    row.workspace_id = repo.workspace_id or LOCAL_WORKSPACE_ID
    row.name = repo.name
    row.created_by = repo.created_by
    row.url = repo.url
    row.source = repo.source
    row.release_date = repo.release_date
    row.interval_days = repo.interval_days
    row.inspect_image = repo.inspect_image
    row.image_tags = dumped["image_tags"]
    row.detected_tag_vars = dumped["detected_tag_vars"]
    row.token_override = crypto.encrypt(repo.token_override) if repo.token_override else None
    row.ignored_packages = list(repo.ignored_packages)
    row.created_at = repo.created_at
    row.last_scan_by_branch = dumped["last_scan_by_branch"]
    s.flush()  # parent row before its branch rows (FK)
    s.execute(delete(RepoBranchRow).where(RepoBranchRow.repo_id == repo.id))
    s.execute(insert(RepoBranchRow), [
        {"repo_id": repo.id, "branch": b, "position": i} for i, b in enumerate(repo.branches)
    ])


def upsert_repo(repo: Repo) -> Repo:
    with session() as s:
        _write_repo(s, repo)
    return repo


def save_repos(repos: list[Repo]) -> None:
    """Replace the whole registry with `repos`, in that order."""
    with session() as s:
        keep = [r.id for r in repos]
        s.execute(delete(RepoRow).where(RepoRow.id.not_in(keep)))
        for i, repo in enumerate(repos, start=1):
            _write_repo(s, repo)
            s.get(RepoRow, repo.id).position = i


def delete_repo(repo_id: str) -> bool:
    """Remove the repo from the registry AND all of its snapshot history
    (packages / enrichment / flags / scan logs cascade with the snapshots)."""
    with session() as s:
        row = s.get(RepoRow, repo_id)
        if row is None:
            return False
        s.execute(delete(SnapshotRow).where(SnapshotRow.repo_id == repo_id))
        s.delete(row)  # repo_branches cascade
    return True


# --------------------------------------------------------------------------- #
# Snapshot index                                                               #
# --------------------------------------------------------------------------- #
def get_or_create_snapshot(s: Session, repo_id: str, branch: str, ts: str) -> SnapshotRow:
    return _snapshot(s, repo_id, branch, ts, create=True)


def apply_summary(row: SnapshotRow, summary: SnapshotSummary) -> None:
    """List `row` in its branch's index with `summary`'s counts."""
    _apply_summary(row, summary)


def _snapshot(s: Session, repo_id: str, branch: str, ts: str, *, create: bool) -> SnapshotRow | None:
    q = select(SnapshotRow).where(
        SnapshotRow.repo_id == repo_id, SnapshotRow.branch == branch, SnapshotRow.ts == ts,
    )
    # writers lock the snapshot row: payload rewrites are delete-then-insert
    row = s.scalar(q.with_for_update() if create else q)
    if row is None and create:
        row = SnapshotRow(repo_id=repo_id, branch=branch, ts=ts, indexed=False,
                          package_count=0, flag_counts={}, degraded=False)
        s.add(row)
        s.flush()
    return row


def _summary(row: SnapshotRow) -> SnapshotSummary:
    return SnapshotSummary(ts=row.ts, package_count=row.package_count,
                           flag_counts=row.flag_counts or {}, degraded=row.degraded,
                           ai_status=row.ai_status, ai_detail=row.ai_detail)


def _apply_summary(row: SnapshotRow, summary: SnapshotSummary) -> None:
    row.indexed = True
    row.package_count = summary.package_count
    row.flag_counts = dict(summary.flag_counts)
    row.degraded = summary.degraded
    row.ai_status = summary.ai_status
    row.ai_detail = summary.ai_detail


def load_index(repo_id: str, branch: str) -> list[SnapshotSummary]:
    """Listed snapshots for a branch, newest first."""
    with session() as s:
        rows = s.scalars(select(SnapshotRow).where(
            SnapshotRow.repo_id == repo_id, SnapshotRow.branch == branch, SnapshotRow.indexed,
        ).order_by(SnapshotRow.ts.desc())).all()
        return [_summary(r) for r in rows]


def save_index(repo_id: str, branch: str, summaries: list[SnapshotSummary]) -> None:
    """Make the branch's index exactly `summaries` (others become unlisted;
    their payloads are kept, as the file version kept their folders)."""
    with session() as s:
        listed = {x.ts for x in summaries}
        s.execute(update(SnapshotRow).where(
            SnapshotRow.repo_id == repo_id, SnapshotRow.branch == branch,
            SnapshotRow.ts.not_in(listed),
        ).values(indexed=False))
        for summary in summaries:
            _apply_summary(_snapshot(s, repo_id, branch, summary.ts, create=True), summary)


def upsert_index_entry(repo_id: str, branch: str, summary: SnapshotSummary) -> None:
    with session() as s:
        _apply_summary(_snapshot(s, repo_id, branch, summary.ts, create=True), summary)


def latest_snapshot_ts(repo_id: str, branch: str) -> str | None:
    with session() as s:
        return s.scalar(select(SnapshotRow.ts).where(
            SnapshotRow.repo_id == repo_id, SnapshotRow.branch == branch, SnapshotRow.indexed,
        ).order_by(SnapshotRow.ts.desc()).limit(1))


# --------------------------------------------------------------------------- #
# Snapshot payloads                                                            #
# --------------------------------------------------------------------------- #
# A write replaces that payload wholesale (re-flagging rewrites flags), and
# creates the snapshot row if this is the scan's first write — the pipeline
# writes payloads before it lists the snapshot in the index.
def write_scan_run(run: ScanRunRecord) -> None:
    """Record one finished scan attempt: the analytics fact row."""
    with session() as s:
        s.add(ScanRunRow(**_strip_nul(run.model_dump())))


def list_scan_runs(repo_id: str | None = None, *, limit: int = 100) -> list[ScanRunRecord]:
    """Recorded scan attempts, newest first, optionally for one repo."""
    with session() as s:
        q = select(ScanRunRow).order_by(ScanRunRow.started_at.desc(), ScanRunRow.id.desc())
        if repo_id is not None:
            q = q.where(ScanRunRow.repo_id == repo_id)
        rows = s.scalars(q.limit(limit)).all()
        return [ScanRunRecord.model_validate(r, from_attributes=True) for r in rows]


def _strip_nul(v):
    """Postgres text/JSONB reject NUL characters, which upstream metadata (an npm
    deprecation message, a scraped summary) can contain. Files stored them; a
    DB write would fail the whole scan, so drop them."""
    if isinstance(v, str):
        return v.replace("\x00", "") if "\x00" in v else v
    if isinstance(v, dict):
        return {_strip_nul(k): _strip_nul(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_strip_nul(x) for x in v]
    return v


def _replace(s: Session, model, snapshot_id: int, rows: list[dict]) -> None:
    s.execute(delete(model).where(model.snapshot_id == snapshot_id))
    if rows:
        s.execute(insert(model), _strip_nul(rows))


def _read_rows(model, repo_id: str, branch: str, ts: str) -> list:
    with session() as s:
        snap = _snapshot(s, repo_id, branch, ts, create=False)
        if snap is None:
            return []
        return s.scalars(select(model).where(model.snapshot_id == snap.id)
                         .order_by(model.position)).all()


def put_packages(s: Session, snap: SnapshotRow, packages: list[Package]) -> None:
    _replace(s, PackageRow, snap.id, [
        {"snapshot_id": snap.id, "position": i, "key": p.key, "name": p.name,
         "version": p.version, "ecosystem": p.ecosystem.value,
         "source_file": p.source_file, "is_direct": p.is_direct}
        for i, p in enumerate(packages)
    ])


def put_enrichment(s: Session, snap: SnapshotRow, items: list[Enrichment]) -> None:
    _replace(s, EnrichmentRow, snap.id, [
        {"snapshot_id": snap.id, "position": i, "key": e.key,
         "ecosystem": e.ecosystem.value, "name": e.name, "version": e.version,
         "verdict": e.verdict.value, "eol_date": e.eol_date,
         "data": e.model_dump(mode="json")}
        for i, e in enumerate(items)
    ])


def put_flags(s: Session, snap: SnapshotRow, flags: list[Flag]) -> None:
    _replace(s, FlagRow, snap.id, [
        {"snapshot_id": snap.id, "position": i, "key": f.key,
         "status": f.status.value, "reasons": list(f.reasons)}
        for i, f in enumerate(flags)
    ])


def put_scan_log(s: Session, snap: SnapshotRow, log: dict) -> None:
    s.merge(ScanLogRow(snapshot_id=snap.id, log=_strip_nul(log)))


def write_packages(repo_id: str, branch: str, ts: str, packages: list[Package]) -> None:
    with session() as s:
        put_packages(s, _snapshot(s, repo_id, branch, ts, create=True), packages)


def read_packages(repo_id: str, branch: str, ts: str) -> list[Package]:
    return [
        Package(name=r.name, version=r.version, ecosystem=r.ecosystem,
                source_file=r.source_file, is_direct=r.is_direct)
        for r in _read_rows(PackageRow, repo_id, branch, ts)
    ]


def write_enrichment(repo_id: str, branch: str, ts: str, items: list[Enrichment]) -> None:
    with session() as s:
        put_enrichment(s, _snapshot(s, repo_id, branch, ts, create=True), items)


def read_enrichment(repo_id: str, branch: str, ts: str) -> list[Enrichment]:
    return [Enrichment(**r.data) for r in _read_rows(EnrichmentRow, repo_id, branch, ts)]


def write_flags(repo_id: str, branch: str, ts: str, flags: list[Flag]) -> None:
    with session() as s:
        put_flags(s, _snapshot(s, repo_id, branch, ts, create=True), flags)


def read_flags(repo_id: str, branch: str, ts: str) -> list[Flag]:
    return [Flag(key=r.key, status=r.status, reasons=r.reasons or [])
            for r in _read_rows(FlagRow, repo_id, branch, ts)]


def write_scan_log(repo_id: str, branch: str, ts: str, log) -> None:
    with session() as s:
        put_scan_log(s, _snapshot(s, repo_id, branch, ts, create=True), log.model_dump(mode="json"))


def read_scan_log(repo_id: str, branch: str, ts: str) -> dict | None:
    with session() as s:
        snap = _snapshot(s, repo_id, branch, ts, create=False)
        if snap is None:
            return None
        row = s.get(ScanLogRow, snap.id)
        return row.log if row else None


# --------------------------------------------------------------------------- #
# CSV report — built on request from the stored snapshot                       #
# --------------------------------------------------------------------------- #
REPORT_COLUMNS = [
    "name", "ecosystem", "version", "latest_version", "latest_stable_version",
    "update_available", "verdict",
    "eol_date", "eos_date", "successor", "security_supported", "confidence", "flag",
    "reasons", "source_file", "is_direct", "sources",
]


def build_report_csv(
    packages: list[Package],
    enrich_by_key: dict[str, Enrichment],
    flag_by_key: dict[str, Flag],
) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=REPORT_COLUMNS)
    w.writeheader()
    for p in packages:
        e = enrich_by_key.get(p.key)
        fl = flag_by_key.get(p.key)
        w.writerow({
            "name": p.name,
            "ecosystem": p.ecosystem.value,
            "version": p.version or "",
            "latest_version": (e.latest_version if e else "") or "",
            "latest_stable_version": (e.latest_stable_version if e else "") or "",
            "update_available": flags.upgrade_available(p, e),
            "verdict": e.verdict.value if e else "",
            "eol_date": (e.eol_date.isoformat() if e and e.eol_date else ""),
            "eos_date": (e.eos_date.isoformat() if e and e.eos_date else ""),
            "successor": (e.successor if e else "") or "",
            "security_supported": "" if not e or e.security_supported is None else e.security_supported,
            "confidence": e.confidence.value if e else "",
            "flag": fl.status.value if fl else "",
            "reasons": "; ".join(fl.reasons) if fl else "",
            "source_file": p.source_file,
            "is_direct": p.is_direct,
            "sources": " ".join(s.url for s in (e.sources if e else [])),
        })
    return buf.getvalue()


def read_report_csv(repo_id: str, branch: str, ts: str) -> str | None:
    """The snapshot's CSV report, or None when no such listed snapshot exists.
    Built from current rows, so it always reflects the latest re-flag."""
    with session() as s:
        snap = _snapshot(s, repo_id, branch, ts, create=False)
        if snap is None or not snap.indexed:
            return None
    packages = read_packages(repo_id, branch, ts)
    enrich_by_key = {e.key: e for e in read_enrichment(repo_id, branch, ts)}
    flag_by_key = {f.key: f for f in read_flags(repo_id, branch, ts)}
    text = build_report_csv(packages, enrich_by_key, flag_by_key)
    # csv writes CRLF; the file version served it back through read_text's
    # universal newlines, so downloads have always been LF — keep it that way.
    return text.replace("\r\n", "\n").replace("\r", "\n")
