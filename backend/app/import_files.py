"""One-off import of the legacy file storage into the database.

    cd backend && python -m app.import_files                # reads ./data (EOL_DATA_DIR)
    python -m app.import_files --data-dir /path/to/data --dry-run

Idempotent and non-destructive:
- repos already in the DB are left alone (the DB copy may have been edited
  since) unless `--overwrite-repos`;
- runtime settings are only filled in where the DB has no value;
- a snapshot already in the DB (same repo, branch, ts) is skipped;
- each snapshot imports in its own transaction, so an interrupted run leaves
  no half-imported snapshot — just re-run it.
The data directory is only read, never modified; keep it as a backup.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from . import db, legacy_files, storage
from .config import get_settings
from .db_models import AppSettingRow, SnapshotRow


@dataclass
class Counts:
    repos_added: int = 0
    repos_skipped: int = 0
    settings_added: int = 0
    snapshots_added: int = 0
    snapshots_skipped: int = 0
    packages: int = 0


def _snapshot_exists(repo_id: str, branch: str, ts: str) -> bool:
    with db.session() as s:
        return s.scalar(select(SnapshotRow.id).where(
            SnapshotRow.repo_id == repo_id, SnapshotRow.branch == branch, SnapshotRow.ts == ts,
        )) is not None


def _import_snapshot(repo_id: str, branch: str, snap: legacy_files.LegacySnapshot) -> int:
    """All payloads of one snapshot in a single transaction. Returns #packages."""
    packages = legacy_files.read_packages(snap.dir)
    enrichment = legacy_files.read_enrichment(snap.dir)
    flags = legacy_files.read_flags(snap.dir)
    log = legacy_files.read_scan_log(snap.dir)
    with db.session() as s:
        row = storage.get_or_create_snapshot(s, repo_id, branch, snap.ts)
        storage.put_packages(s, row, packages)
        storage.put_enrichment(s, row, enrichment)
        storage.put_flags(s, row, flags)
        if log is not None:
            storage.put_scan_log(s, row, log)
        if snap.indexed and snap.summary is not None:
            storage.apply_summary(row, snap.summary)
    return len(packages)


def _schema_ready() -> bool:
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(db.alembic_config()).get_current_head()
    with db.get_engine().connect() as conn:
        return MigrationContext.configure(conn).get_current_revision() == head


def run(data_dir: Path, *, overwrite_repos: bool = False, dry_run: bool = False,
        empty_db: bool = False, out=sys.stdout) -> Counts:
    """`empty_db` (dry runs against a DB with no schema yet): treat the DB as
    empty instead of reading it."""
    c = Counts()
    say = lambda msg: print(msg, file=out)  # noqa: E731
    if empty_db:
        exists = lambda *_: False  # noqa: E731
    else:
        exists = _snapshot_exists

    existing = set() if empty_db else {r.id for r in storage.load_repos()}
    for repo in legacy_files.load_repos(data_dir):
        if repo.id in existing and not overwrite_repos:
            c.repos_skipped += 1
            say(f"repo {repo.id}: already in DB, kept")
            continue
        c.repos_added += 1
        say(f"repo {repo.id}: import ({', '.join(repo.branches)})")
        if not dry_run:
            storage.upsert_repo(repo)

    legacy_settings = legacy_files.load_settings(data_dir)
    if empty_db:
        c.settings_added = len(legacy_settings)
    else:
        with db.session() as s:
            have = {r.key for r in s.scalars(select(AppSettingRow))}
            for k, v in legacy_settings.items():
                if k not in have:
                    c.settings_added += 1
                    if not dry_run:
                        s.add(AppSettingRow(key=k, value=v))

    for lb in legacy_files.discover(data_dir):
        say(f"history {lb.repo_id} @ {lb.branch}: {len(lb.snapshots)} snapshot(s)")
        for snap in lb.snapshots:
            if exists(lb.repo_id, lb.branch, snap.ts):
                c.snapshots_skipped += 1
                continue
            c.snapshots_added += 1
            if dry_run:
                c.packages += len(legacy_files.read_packages(snap.dir))
                continue
            c.packages += _import_snapshot(lb.repo_id, lb.branch, snap)

    say(
        f"{'DRY RUN — nothing written. ' if dry_run else ''}"
        f"repos: {c.repos_added} imported, {c.repos_skipped} kept | "
        f"settings: {c.settings_added} imported | "
        f"snapshots: {c.snapshots_added} imported, {c.snapshots_skipped} already present | "
        f"packages: {c.packages}"
    )
    if c.repos_added and not dry_run:
        say("Restart the backend (systemctl restart packages-tracker-backend) so the "
            "imported repos get their scheduled scans.")
    return c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="legacy data directory (default: EOL_DATA_DIR, i.e. ./data)")
    ap.add_argument("--overwrite-repos", action="store_true",
                    help="replace repos that already exist in the DB with the file version")
    ap.add_argument("--dry-run", action="store_true", help="report what would be imported")
    args = ap.parse_args(argv)
    data_dir = args.data_dir or get_settings().data_dir
    if not (data_dir / "repos.json").exists() and not (data_dir / "snapshots").exists():
        print(f"no legacy data found in {data_dir}", file=sys.stderr)
        return 1
    if args.dry_run:
        # never create or migrate the schema on a dry run
        empty = not _schema_ready()
        if empty:
            print("database schema not created yet — dry run assumes an empty database")
        run(data_dir, overwrite_repos=args.overwrite_repos, dry_run=True, empty_db=empty)
    else:
        db.upgrade()
        run(data_dir, overwrite_repos=args.overwrite_repos)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
