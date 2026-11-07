"""Read-only access to the pre-database file storage, for `import_files`.

Layout written by every version before the database:

    data/
    ├── repos.json
    ├── app_settings.json
    └── snapshots/<repo_id>/<branch-slug>/
        ├── index.json
        └── <ts>/{packages,enrichment,flags,scan_log}.json + report.csv

Pre-multi-branch data has `index.json` and the ts folders directly under
`snapshots/<repo_id>/`; that history belongs to the repo's first branch.
Nothing here writes or moves files — the tree stays intact as a backup.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Enrichment, Flag, Package, Repo, SnapshotSummary
from .storage import branch_dir_name


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_repos(root: Path) -> list[Repo]:
    return [Repo(**r) for r in read_json(root / "repos.json", default=[])]


def load_settings(root: Path) -> dict:
    return read_json(root / "app_settings.json", default={}) or {}


@dataclass
class LegacySnapshot:
    ts: str
    indexed: bool
    summary: SnapshotSummary | None
    dir: Path


@dataclass
class LegacyBranch:
    repo_id: str
    branch: str
    dir: Path
    snapshots: list[LegacySnapshot] = field(default_factory=list)


def _is_ts_dir(p: Path) -> bool:
    return p.is_dir() and len(p.name) >= 20 and p.name[:4].isdigit() and p.name.endswith("Z")


def _branch_name(repo: Repo | None, slug: Path) -> str:
    """Branch dirs are slugs (`release/6.1` → `release_6.1`); recover the real
    name from the repo's branches, else from a stored scan log, else the slug."""
    for b in (repo.branches if repo else []):
        if branch_dir_name(b) == slug.name:
            return b
    for log in sorted(slug.glob("*/scan_log.json"), reverse=True):
        name = (read_json(log, default={}) or {}).get("branch")
        if name and branch_dir_name(name) == slug.name:
            return name
    return slug.name


def _collect(repo_id: str, branch: str, d: Path) -> LegacyBranch:
    lb = LegacyBranch(repo_id=repo_id, branch=branch, dir=d)
    index = {s["ts"]: SnapshotSummary(**s) for s in read_json(d / "index.json", default=[])}
    ts_dirs = {p.name for p in d.iterdir() if _is_ts_dir(p)}
    for ts in sorted(ts_dirs | set(index)):
        lb.snapshots.append(LegacySnapshot(
            ts=ts, indexed=ts in index, summary=index.get(ts), dir=d / ts,
        ))
    return lb


def discover(root: Path) -> list[LegacyBranch]:
    """Every (repo, branch) history on disk, including repos no longer in
    repos.json."""
    repos = {r.id: r for r in load_repos(root)}
    snaps_root = root / "snapshots"
    out: list[LegacyBranch] = []
    if not snaps_root.exists():
        return out
    for repo_dir in sorted(p for p in snaps_root.iterdir() if p.is_dir()):
        repo = repos.get(repo_dir.name)
        if (repo_dir / "index.json").exists():  # pre-multi-branch flat layout
            branch = repo.branches[0] if repo else "main"
            out.append(_collect(repo_dir.name, branch, repo_dir))
        for sub in sorted(p for p in repo_dir.iterdir() if p.is_dir() and not _is_ts_dir(p)):
            out.append(_collect(repo_dir.name, _branch_name(repo, sub), sub))
    return out


def read_packages(snap_dir: Path) -> list[Package]:
    return [Package(**p) for p in read_json(snap_dir / "packages.json", default=[])]


def read_enrichment(snap_dir: Path) -> list[Enrichment]:
    return [Enrichment(**e) for e in read_json(snap_dir / "enrichment.json", default=[])]


def read_flags(snap_dir: Path) -> list[Flag]:
    return [Flag(**f) for f in read_json(snap_dir / "flags.json", default=[])]


def read_scan_log(snap_dir: Path) -> dict | None:
    return read_json(snap_dir / "scan_log.json", default=None)
