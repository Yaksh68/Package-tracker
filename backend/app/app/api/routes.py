"""REST API — reads/writes the JSON files, drives scans, serves reports."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import ValidationError
from fastapi.responses import PlainTextResponse

from .. import flags as flags_mod
from .. import scanlog, scheduler, settings_store, storage, uploads
from ..models import (
    Repo,
    RepoCreate,
    RepoUpdate,
    ScanStatus,
    UploadRepoCreate,
    upload_version,
)
from ..pipeline import clear_repo_state, get_status, is_scanning, run_scan

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# runtime settings (UI-editable)                                               #
# --------------------------------------------------------------------------- #
@router.get("/settings")
def get_settings_route() -> dict:
    return settings_store.get_runtime_settings()


@router.put("/settings")
def update_settings_route(patch: dict) -> dict:
    return settings_store.update_runtime_settings(patch)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _mask(repo: Repo) -> dict:
    d = repo.model_dump(mode="json")
    if repo.token_override:
        d["token_override"] = "****"
    return d


def _require_repo(repo_id: str) -> Repo:
    repo = storage.get_repo(repo_id)
    if repo is None:
        raise HTTPException(404, f"repo '{repo_id}' not found")
    return repo


def _require_branch(repo: Repo, branch: str) -> str:
    if branch not in repo.branches:
        raise HTTPException(404, f"repo '{repo.id}' is not tracking branch '{branch}'")
    return branch


# --------------------------------------------------------------------------- #
# repos CRUD                                                                    #
# --------------------------------------------------------------------------- #
@router.get("/repos")
def list_repos() -> list[dict]:
    out = []
    for r in storage.load_repos():
        d = _mask(r)
        branches_info = []
        for b in r.branches:
            idx = storage.load_index(r.id, b)
            last_scan = r.last_scan_by_branch.get(b)
            status = get_status(r.id, b)
            nxt = scheduler.next_run(r.id, b)
            branches_info.append({
                "branch": b,
                "latest_snapshot": idx[0].ts if idx else None,
                "flag_counts": idx[0].flag_counts if idx else {},
                "scan_state": status.state.value,
                "last_scan": last_scan.isoformat() if last_scan else None,
                "next_scan": nxt.isoformat() if nxt else None,
                # Degraded survives a restart via the snapshot index; the issue
                # detail behind it only lives in this process's status (and, for
                # any snapshot, in its persisted scan log).
                "degraded": bool(idx and idx[0].degraded) or status.degraded,
                "issues": [i.model_dump() for i in status.issues],
            })
        d["branches_info"] = branches_info
        out.append(d)
    return out


@router.post("/repos", status_code=201)
async def create_repo(body: RepoCreate) -> dict:
    repo_id = body.id or storage.slugify_repo_id(body.url)
    if storage.get_repo(repo_id):
        raise HTTPException(409, f"repo '{repo_id}' already exists")
    repo = Repo(
        id=repo_id,
        url=body.url,
        branches=body.branches,
        release_date=body.release_date,
        interval_days=body.interval_days,
        inspect_image=body.inspect_image,
        image_tags=body.image_tags,
        token_override=body.token_override,
        ignored_packages=body.ignored_packages,
        created_at=datetime.now(timezone.utc),
    )
    storage.upsert_repo(repo)
    scheduler.sync_repo(repo)
    for branch in repo.branches:
        scheduler.trigger_now(repo, branch)  # first scan starts immediately, per branch
    return _mask(repo)


@router.get("/repos/{repo_id}")
def get_repo(repo_id: str) -> dict:
    return _mask(_require_repo(repo_id))


@router.put("/repos/{repo_id}")
def update_repo(repo_id: str, body: RepoUpdate) -> dict:
    repo = _require_repo(repo_id)
    updates = body.model_dump(exclude_unset=True)
    reflag = "release_date" in updates or (
        "ignored_packages" in updates and updates["ignored_packages"] != repo.ignored_packages
    )
    removed = set(repo.branches) - set(updates.get("branches") or repo.branches)
    repo = repo.model_copy(update=updates)
    storage.upsert_repo(repo)
    scheduler.sync_repo(repo)
    if repo.source == "upload":
        for version in removed:
            uploads.delete(repo.id, version)

    # PUT release_date / ignored_packages → re-flag every branch's latest
    # snapshot instantly (no re-scan, no AI)
    if reflag:
        for branch in repo.branches:
            _reflag_latest(repo, branch)
    return _mask(repo)


@router.delete("/repos/{repo_id}", status_code=204)
def delete_repo(repo_id: str) -> Response:
    repo = _require_repo(repo_id)
    if any(is_scanning(repo_id, b) for b in repo.branches):
        raise HTTPException(409, "a scan is in progress; try again once it finishes")
    if not storage.delete_repo(repo_id):
        raise HTTPException(404, f"repo '{repo_id}' not found")
    scheduler.unsync_repo(repo_id)      # stop every branch's interval job
    uploads.delete(repo_id)             # uploaded zips, if any
    clear_repo_state(repo_id)           # drop in-memory scan status + lock (all branches)
    scanlog.clear_repo(repo_id)         # drop in-memory activity trace (all branches)
    return Response(status_code=204)


def _reflag_latest(repo: Repo, branch: str) -> None:
    ts = storage.latest_snapshot_ts(repo.id, branch)
    if not ts:
        return
    packages = storage.read_packages(repo.id, branch, ts)
    enrich_by_key = {e.key: e for e in storage.read_enrichment(repo.id, branch, ts)}
    flag_list = flags_mod.evaluate_all(packages, enrich_by_key, repo.release_date,
                                       repo.ignored_packages)
    storage.write_flags(repo.id, branch, ts, flag_list)
    idx = storage.load_index(repo.id, branch)
    for s in idx:
        if s.ts == ts:
            s.flag_counts = flags_mod.count_flags(flag_list)
    storage.save_index(repo.id, branch, idx)


# --------------------------------------------------------------------------- #
# scans                                                                         #
# --------------------------------------------------------------------------- #
@router.post("/repos/{repo_id}/scan", status_code=202)
async def scan_now(repo_id: str, branch: str = Query(...)) -> dict:
    repo = _require_repo(repo_id)
    _require_branch(repo, branch)
    if is_scanning(repo_id, branch):
        raise HTTPException(409, "a scan is already in progress")
    if repo.source == "upload" and not uploads.has_upload(repo_id, branch):
        raise HTTPException(400, f"no zip uploaded for version '{branch}' yet")
    scheduler.trigger_now(repo, branch)
    return {"status": "queued", "repo_id": repo_id, "branch": branch}


# --------------------------------------------------------------------------- #
# uploaded zips (source="upload" repos)                                         #
# --------------------------------------------------------------------------- #
async def _store_zip(repo_id: str, version: str, file: UploadFile) -> None:
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "upload a .zip file")
    try:
        await asyncio.to_thread(uploads.save, repo_id, version, file.file)
    except uploads.UploadError as e:
        raise HTTPException(400, f"zip rejected: {e}") from e


@router.post("/repos/upload", status_code=201)
async def create_upload_repo(file: UploadFile = File(...), meta: str = Form(...)) -> dict:
    """Add a repo scanned from an uploaded zip. `meta` is UploadRepoCreate as JSON."""
    try:
        body = UploadRepoCreate.model_validate_json(meta)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    repo_id = storage.slugify_repo_id(body.name)
    if storage.get_repo(repo_id):
        raise HTTPException(409, f"repo '{repo_id}' already exists")
    version = upload_version(body.version)
    try:
        await _store_zip(repo_id, version, file)
    except HTTPException:
        uploads.delete(repo_id)
        raise
    repo = Repo(
        id=repo_id,
        source="upload",
        url=f"upload://{file.filename}",
        branches=[version],
        release_date=body.release_date,
        interval_days=body.interval_days,
        image_tags=body.image_tags,
        ignored_packages=body.ignored_packages,
        created_at=datetime.now(timezone.utc),
    )
    storage.upsert_repo(repo)
    scheduler.sync_repo(repo)
    scheduler.trigger_now(repo, version)
    return _mask(repo)


@router.post("/repos/{repo_id}/upload")
async def upload_version_zip(
    repo_id: str, file: UploadFile = File(...), version: str | None = Form(None),
) -> dict:
    """Upload a zip for a new version, or replace an existing version's zip,
    then scan it."""
    repo = _require_repo(repo_id)
    if repo.source != "upload":
        raise HTTPException(400, f"repo '{repo_id}' is scanned from git, not from uploads")
    version = upload_version(version)
    if is_scanning(repo_id, version):
        raise HTTPException(409, "a scan of this version is in progress; try again once it finishes")
    await _store_zip(repo_id, version, file)
    branches = repo.branches if version in repo.branches else [*repo.branches, version]
    repo = repo.model_copy(update={"branches": branches, "url": f"upload://{file.filename}"})
    storage.upsert_repo(repo)
    scheduler.sync_repo(repo)
    scheduler.trigger_now(repo, version)
    return _mask(repo)


@router.get("/repos/{repo_id}/scan-status", response_model=ScanStatus)
def scan_status(repo_id: str, branch: str = Query(...)) -> ScanStatus:
    repo = _require_repo(repo_id)
    _require_branch(repo, branch)
    return get_status(repo_id, branch)


@router.get("/repos/{repo_id}/scan-log")
def scan_log(repo_id: str, branch: str = Query(...), ts: str | None = None) -> dict:
    """Activity trace. Defaults to the in-memory log of the current/most-recent
    run; pass ?ts=<snapshot> to read a persisted trace from that snapshot."""
    repo = _require_repo(repo_id)
    _require_branch(repo, branch)
    if ts:
        persisted = storage.read_scan_log(repo_id, branch, ts)
        if persisted is None:
            raise HTTPException(404, f"no scan log for snapshot '{ts}'")
        return persisted
    live = scanlog.get_repo_log(repo_id, branch)
    if live is not None:
        return live.to_model().model_dump(mode="json")
    # fall back to the latest persisted trace
    latest = storage.latest_snapshot_ts(repo_id, branch)
    if latest:
        persisted = storage.read_scan_log(repo_id, branch, latest)
        if persisted is not None:
            return persisted
    return {"repo_id": repo_id, "branch": branch, "snapshot_ts": None, "state": "idle", "entries": []}


# --------------------------------------------------------------------------- #
# snapshots                                                                     #
# --------------------------------------------------------------------------- #
@router.get("/repos/{repo_id}/snapshots")
def list_snapshots(repo_id: str, branch: str = Query(...)) -> list[dict]:
    repo = _require_repo(repo_id)
    _require_branch(repo, branch)
    return [s.model_dump(mode="json") for s in storage.load_index(repo_id, branch)]


@router.get("/repos/{repo_id}/snapshots/{ts}/components")
def components(
    repo_id: str,
    ts: str,
    branch: str = Query(...),
    ecosystem: str | None = None,
    status: str | None = None,
    search: str | None = None,
    direct_only: bool = Query(False),
) -> list[dict]:
    repo = _require_repo(repo_id)
    _require_branch(repo, branch)
    packages = storage.read_packages(repo_id, branch, ts)
    if not packages and ts not in {s.ts for s in storage.load_index(repo_id, branch)}:
        raise HTTPException(404, f"snapshot '{ts}' not found")
    enrich_by_key = {e.key: e for e in storage.read_enrichment(repo_id, branch, ts)}
    flag_by_key = {f.key: f for f in storage.read_flags(repo_id, branch, ts)}

    rows = []
    for p in packages:
        e = enrich_by_key.get(p.key)
        fl = flag_by_key.get(p.key)
        if ecosystem and p.ecosystem.value != ecosystem:
            continue
        if status and (not fl or fl.status.value != status):
            continue
        if direct_only and not p.is_direct:
            continue
        if search and search.lower() not in p.name.lower():
            continue
        rows.append({
            "name": p.name,
            "ecosystem": p.ecosystem.value,
            "version": p.version,
            "is_direct": p.is_direct,
            "source_file": p.source_file,
            "latest_version": e.latest_version if e else None,
            "latest_stable_version": e.latest_stable_version if e else None,
            # derived live (not stored), so existing snapshots get it for free
            "update_available": flags_mod.upgrade_available(p, e),
            "verdict": e.verdict.value if e else None,
            "eol_date": e.eol_date.isoformat() if e and e.eol_date else None,
            "eos_date": e.eos_date.isoformat() if e and e.eos_date else None,
            "successor": e.successor if e else None,
            "security_supported": e.security_supported if e else None,
            "security_policy_row": e.security_policy_row if e else None,
            "confidence": e.confidence.value if e else None,
            "summary": e.summary if e else None,
            "ai_researched": bool(e and e.ai_checked),
            "sources": [s.model_dump() for s in e.sources] if e else [],
            "flag": fl.status.value if fl else None,
            "reasons": fl.reasons if fl else [],
        })
    return rows


@router.get("/repos/{repo_id}/snapshots/{ts}/report.csv")
def report_csv(repo_id: str, ts: str, branch: str = Query(...)) -> Response:
    repo = _require_repo(repo_id)
    _require_branch(repo, branch)
    csv_text = storage.read_report_csv(repo_id, branch, ts)
    if csv_text is None:
        raise HTTPException(404, "report not found")
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="{repo_id}-{storage.branch_dir_name(branch)}-{ts}.csv"'
        },
    )


@router.get("/repos/{repo_id}/diff")
def diff(
    repo_id: str,
    branch: str = Query(...),
    from_ts: str = Query(alias="from"),
    to_ts: str = Query(alias="to"),
) -> dict:
    repo = _require_repo(repo_id)
    _require_branch(repo, branch)
    old = {f.key: f for f in storage.read_flags(repo_id, branch, from_ts)}
    new = {f.key: f for f in storage.read_flags(repo_id, branch, to_ts)}
    old_pkgs = {p.key: p for p in storage.read_packages(repo_id, branch, from_ts)}
    new_pkgs = {p.key: p for p in storage.read_packages(repo_id, branch, to_ts)}

    changed, added, removed = [], [], []
    for key, f in new.items():
        if key not in old:
            p = new_pkgs.get(key)
            added.append({"name": p.name if p else key, "flag": f.status.value})
        elif old[key].status != f.status:
            p = new_pkgs.get(key)
            changed.append({
                "name": p.name if p else key,
                "from": old[key].status.value,
                "to": f.status.value,
            })
    for key in old:  # snapshot order, so the response is stable across restarts
        if key in new:
            continue
        p = old_pkgs.get(key)
        removed.append({"name": p.name if p else key, "flag": old[key].status.value})
    return {"from": from_ts, "to": to_ts, "changed": changed, "added": added, "removed": removed}
