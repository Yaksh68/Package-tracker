"""REST API: repos, scans, uploads, snapshots, reports, per-workspace settings
and secrets, and the signed-in account.

Every route resolves the caller (auth.get_principal). In the local mode
that's the built-in local user and workspace; in the public mode it's the
signed-in user and their active workspace. Each route's permission is
declared in rbac.ROUTE_PERMS and checked by the router's `authorize`
dependency before the handler runs. A repo in a workspace the caller isn't
in is a 404, so its existence never leaks. Mutating routes also write an
audit entry (audit.py).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from .. import accounts, audit, rbac, scanlog, scheduler, settings_store, storage, uploads
from .. import flags as flags_mod
from ..ai import probe
from ..ai.errors import AiError, redact_keys
from ..auth import Principal, get_principal, session_cookie_name
from ..config import get_settings
from ..git_rules import RepoUrlError, check_branch
from ..models import (
    Repo,
    RepoCreate,
    RepoUpdate,
    ScanStatus,
    UploadRepoCreate,
    upload_version,
)
from ..pipeline import get_status, is_scanning, run_scan  # noqa: F401 - run_scan re-exported
from .common import delete_user_everything, forget_repo

router = APIRouter(prefix="/api", dependencies=[Depends(rbac.authorize)])


def _event(me: Principal, event: str, **meta) -> None:
    accounts.record_event(event, workspace_id=me.workspace_id, user_id=me.user.id, **meta)


def _audit(me: Principal, action: str, *, workspace_id: str | None = None,
           target_type: str | None = None, target_id: str | None = None, **details) -> None:
    audit.record(action, workspace_id=workspace_id or me.workspace_id, actor=me,
                 target_type=target_type, target_id=target_id, **details)


# --------------------------------------------------------------------------- #
# the signed-in account                                                         #
# --------------------------------------------------------------------------- #
@router.get("/me")
def me_route(me: Principal = Depends(get_principal)) -> dict:
    return {
        "mode": "github" if get_settings().public_mode() else "local",
        "user": me.user.model_dump(mode="json"),
        "workspace_id": me.workspace_id,
        "role": me.role,
        # what the caller may do in the active workspace (the UI disables the rest)
        "permissions": rbac.permissions(me.role),
        # what each role may do, so the UI can name the role a control needs
        # and judge a repo from another of the caller's workspaces
        "role_permissions": {r: rbac.permissions(r) for r in rbac.ROLES},
        "workspaces": [w.model_dump() for w in me.workspaces],
    }


@router.delete("/me", status_code=204)
def delete_account(me: Principal = Depends(get_principal)) -> Response:
    """Delete the signed-in user, their personal workspace and any team
    workspace they're the only member of (repos, history, uploads, settings,
    secrets), and their analytics rows. Refused while they're the last Owner
    of a team workspace that has other members."""
    if not get_settings().public_mode():
        raise HTTPException(400, "there are no accounts to delete in the local mode")
    for w in me.workspaces:
        if (not w.personal and w.role == "owner" and accounts.owner_count(w.id) == 1
                and len(accounts.workspace_members(w.id)) > 1):
            raise HTTPException(409, f"you're the last Owner of '{w.name}', which has other "
                                     "members: make someone else Owner first")
    delete_user_everything(me.user.id)  # sessions, memberships and activity cascade
    resp = Response(status_code=204)
    resp.delete_cookie(session_cookie_name(), path="/")
    return resp


# --------------------------------------------------------------------------- #
# runtime settings and secrets (per workspace)                                  #
# --------------------------------------------------------------------------- #
def _secrets_view(me: Principal) -> dict:
    """Which secrets are set, with hints, for roles that manage secrets; just
    set / not set for everyone else."""
    hints = accounts.secret_hints(me.workspace_id)
    if rbac.can(me.role, rbac.Perm.SECRETS):
        return hints
    return {name: {"set": h["set"], "hint": None, "updated_at": None} for name, h in hints.items()}


@router.get("/settings")
def get_settings_route(me: Principal = Depends(get_principal)) -> dict:
    """The workspace's settings, plus whether each secret is set (and its
    hint, for Admins and Owners). Secret values are never returned."""
    return {**settings_store.get_runtime_settings(me.workspace_id), "secrets": _secrets_view(me)}


@router.put("/settings")
def update_settings_route(patch: dict, me: Principal = Depends(get_principal)) -> dict:
    try:
        out = settings_store.update_runtime_settings(patch, me.workspace_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    changed = {k: out[k] for k in patch if k in out}
    _audit(me, "settings.update", target_type="workspace", target_id=me.workspace_id, **changed)
    if "use_enrichment_agent" in patch:
        _event(me, "ai_toggle", on=bool(out["use_enrichment_agent"]))
    return {**out, "secrets": _secrets_view(me)}


class AiTestBody(BaseModel):
    provider: str = Field(max_length=40)
    base_url: str | None = Field(default=None, max_length=500)
    # a key typed into the form and not saved yet; without one, the saved key
    api_key: str | None = Field(default=None, max_length=4096)


@router.post("/settings/ai-test")
async def test_ai_connection(body: AiTestBody, me: Principal = Depends(get_principal)) -> dict:
    """"Test connection": the models the provider offers with this key, or
    why it refused. Uses the key in the body (which isn't saved), or else the
    workspace's saved key. Listing models costs no tokens."""
    base_url = (body.base_url or "").strip() if body.provider == "openai-compatible" else ""
    if base_url:
        try:
            await asyncio.to_thread(settings_store.check_ai_base_url, base_url)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    key = (body.api_key or "").strip() or await asyncio.to_thread(
        accounts.get_secret, me.workspace_id, "ai_api_key")
    try:
        models = await probe.test_connection(body.provider, api_key=key, base_url=base_url or None)
    except AiError as e:
        return {"ok": False, "kind": e.kind, "message": redact_keys(str(e)), "models": []}
    return {"ok": True, "models": models}


class SecretValue(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


def _secret_name(name: str) -> str:
    if name not in accounts.SECRET_NAMES:
        raise HTTPException(404, f"unknown secret '{name}'")
    return name


@router.put("/secrets/{name}")
def set_secret_route(name: str, body: SecretValue, me: Principal = Depends(get_principal)) -> dict:
    """Save a workspace secret (an AI or search key, a git token). Write-only:
    the response, like every later read, carries only a hint."""
    hint = accounts.set_secret(me.workspace_id, _secret_name(name), body.value.strip(),
                               by=me.user.id)
    _audit(me, "secret.set", target_type="secret", target_id=name)  # the name only, never the value
    _event(me, "secret_set", name=name)
    return {"name": name, "set": True, "hint": hint}


@router.delete("/secrets/{name}", status_code=204)
def delete_secret_route(name: str, me: Principal = Depends(get_principal)) -> Response:
    if accounts.delete_secret(me.workspace_id, _secret_name(name)):
        _audit(me, "secret.remove", target_type="secret", target_id=name)
        _event(me, "secret_removed", name=name)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# helpers                                                                       #
# --------------------------------------------------------------------------- #
def _mask(repo: Repo) -> dict:
    d = repo.model_dump(mode="json")
    if repo.token_override:
        d["token_override"] = "****"
    return d


def _require_repo(repo_id: str, me: Principal) -> Repo:
    repo = storage.get_repo(repo_id)
    if repo is None or not me.member_of(repo.workspace_id):
        raise HTTPException(404, f"repo '{repo_id}' not found")
    return repo


def _require_branch(repo: Repo, branch: str) -> str:
    if branch not in repo.branches:
        raise HTTPException(404, f"repo '{repo.id}' is not tracking branch '{branch}'")
    return branch


def _new_repo_id(slug_source: str) -> str:
    # The local mode keeps readable slugs. The public mode uses random IDs: a
    # slug collides as soon as two users add the same repo.
    if get_settings().public_mode():
        return storage.new_repo_id()
    return storage.slugify_repo_id(slug_source)


# --------------------------------------------------------------------------- #
# repos CRUD                                                                    #
# --------------------------------------------------------------------------- #
@router.get("/repos")
def list_repos(me: Principal = Depends(get_principal)) -> list[dict]:
    out = []
    for r in storage.load_repos(me.workspace_id):
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
async def create_repo(body: RepoCreate, me: Principal = Depends(get_principal)) -> dict:
    repo_id = (body.id if body.id and not get_settings().public_mode()
               else _new_repo_id(body.url))
    if storage.get_repo(repo_id):
        raise HTTPException(409, f"repo '{repo_id}' already exists")
    repo = Repo(
        id=repo_id,
        workspace_id=me.workspace_id,
        name=storage.slugify_repo_id(body.url),
        created_by=me.user.id,
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
    scheduler.sync_repo(repo, scanning_now=set(repo.branches))
    for branch in repo.branches:
        # first scan starts immediately, per branch
        scheduler.trigger_now(repo, branch, trigger="initial")
    _audit(me, "repo.create", target_type="repo", target_id=repo.id, name=repo.name, url=repo.url,
           branches=repo.branches, token_set=bool(repo.token_override))
    _event(me, "repo_added", source="git", branches=len(repo.branches))
    return _mask(repo)


@router.get("/repos/{repo_id}")
def get_repo(repo_id: str, me: Principal = Depends(get_principal)) -> dict:
    return _mask(_require_repo(repo_id, me))


@router.put("/repos/{repo_id}")
def update_repo(repo_id: str, body: RepoUpdate, me: Principal = Depends(get_principal)) -> dict:
    repo = _require_repo(repo_id, me)
    updates = body.model_dump(exclude_unset=True)
    # A git repo's branches go to `git clone`; upload "branches" are free-form
    # version labels, so only git repos get the ref-name check.
    if repo.source == "git" and updates.get("branches"):
        try:
            updates["branches"] = [check_branch(b) for b in updates["branches"]]
        except RepoUrlError as e:
            raise HTTPException(422, str(e)) from e
    ignored_changed = ("ignored_packages" in updates
                       and updates["ignored_packages"] != repo.ignored_packages)
    reflag = "release_date" in updates or ignored_changed
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
    # what changed, with values for everything but the token
    changes = {k: ("set" if v else "removed") if k == "token_override" else v
               for k, v in body.model_dump(mode="json", exclude_unset=True).items()}
    _audit(me, "repo.update", workspace_id=repo.workspace_id, target_type="repo",
           target_id=repo.id, **changes)
    if "release_date" in updates:
        _event(me, "release_date_edit")
    if ignored_changed:
        _event(me, "ignored_packages_edit", count=len(repo.ignored_packages))
    return _mask(repo)


@router.delete("/repos/{repo_id}", status_code=204)
def delete_repo(repo_id: str, me: Principal = Depends(get_principal)) -> Response:
    repo = _require_repo(repo_id, me)
    if any(is_scanning(repo_id, b) for b in repo.branches):
        raise HTTPException(409, "a scan is in progress; try again once it finishes")
    forget_repo(repo_id)
    _audit(me, "repo.delete", workspace_id=repo.workspace_id, target_type="repo",
           target_id=repo.id, name=repo.name, url=repo.url)
    _event(me, "repo_deleted")
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
async def scan_now(repo_id: str, branch: str = Query(...),
                   me: Principal = Depends(get_principal)) -> dict:
    repo = _require_repo(repo_id, me)
    _require_branch(repo, branch)
    if is_scanning(repo_id, branch):
        raise HTTPException(409, "a scan is already in progress")
    if repo.source == "upload" and not uploads.has_upload(repo_id, branch):
        raise HTTPException(400, f"no zip uploaded for version '{branch}' yet")
    scheduler.trigger_now(repo, branch, trigger="manual")
    _audit(me, "scan.manual", workspace_id=repo.workspace_id, target_type="repo",
           target_id=repo.id, branch=branch)
    _event(me, "manual_scan")
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
async def create_upload_repo(file: UploadFile = File(...), meta: str = Form(...),
                             me: Principal = Depends(get_principal)) -> dict:
    """Add a repo scanned from an uploaded zip. `meta` is UploadRepoCreate as JSON."""
    try:
        body = UploadRepoCreate.model_validate_json(meta)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    repo_id = _new_repo_id(body.name)
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
        workspace_id=me.workspace_id,
        name=body.name,
        created_by=me.user.id,
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
    scheduler.sync_repo(repo, scanning_now={version})
    scheduler.trigger_now(repo, version, trigger="upload")
    _audit(me, "repo.create", target_type="repo", target_id=repo.id, name=repo.name,
           source="upload", version=version)
    _event(me, "repo_added", source="upload")
    _event(me, "zip_upload")
    return _mask(repo)


@router.post("/repos/{repo_id}/upload")
async def upload_version_zip(
    repo_id: str, file: UploadFile = File(...), version: str | None = Form(None),
    me: Principal = Depends(get_principal),
) -> dict:
    """Upload a zip for a new version, or replace an existing version's zip,
    then scan it."""
    repo = _require_repo(repo_id, me)
    if repo.source != "upload":
        raise HTTPException(400, f"repo '{repo_id}' is scanned from git, not from uploads")
    version = upload_version(version)
    if is_scanning(repo_id, version):
        raise HTTPException(409, "a scan of this version is in progress; try again once it finishes")
    await _store_zip(repo_id, version, file)
    branches = repo.branches if version in repo.branches else [*repo.branches, version]
    repo = repo.model_copy(update={"branches": branches, "url": f"upload://{file.filename}"})
    storage.upsert_repo(repo)
    scheduler.sync_repo(repo, scanning_now={version})
    scheduler.trigger_now(repo, version, trigger="upload")
    _audit(me, "zip.upload", workspace_id=repo.workspace_id, target_type="repo",
           target_id=repo.id, version=version)
    _event(me, "zip_upload")
    return _mask(repo)


@router.get("/repos/{repo_id}/scan-status", response_model=ScanStatus)
def scan_status(repo_id: str, branch: str = Query(...),
                me: Principal = Depends(get_principal)) -> ScanStatus:
    repo = _require_repo(repo_id, me)
    _require_branch(repo, branch)
    return get_status(repo_id, branch)


@router.get("/repos/{repo_id}/scan-log")
def scan_log(repo_id: str, branch: str = Query(...), ts: str | None = None,
             me: Principal = Depends(get_principal)) -> dict:
    """Activity trace. Defaults to the in-memory log of the current/most-recent
    run; pass ?ts=<snapshot> to read a persisted trace from that snapshot."""
    repo = _require_repo(repo_id, me)
    _require_branch(repo, branch)
    log = _scan_log(repo_id, branch, ts)
    return {**log, "ai_usage": _ai_usage(repo_id, branch, log.get("snapshot_ts"))}


def _scan_log(repo_id: str, branch: str, ts: str | None) -> dict:
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


def _ai_usage(repo_id: str, branch: str, ts: str | None) -> dict | None:
    """What that scan's AI research used, from its scan_runs row. None while
    the scan is still running, or for scans from before scan_runs existed."""
    run = storage.scan_run_for(repo_id, branch, ts) if ts else None
    if run is None:
        return None
    return {"status": run.ai_status, "detail": run.ai_detail, "provider": run.ai_provider,
            "model": run.ai_model, "batches": run.ai_batches, "failed_batches": run.ai_failed_batches,
            "tokens_in": run.ai_tokens_in, "tokens_out": run.ai_tokens_out,
            "searches": run.ai_searches, "cost_usd": run.ai_cost_usd,
            "seconds": run.stage_seconds.get("ai")}


# --------------------------------------------------------------------------- #
# snapshots                                                                     #
# --------------------------------------------------------------------------- #
@router.get("/repos/{repo_id}/snapshots")
def list_snapshots(repo_id: str, branch: str = Query(...),
                   me: Principal = Depends(get_principal)) -> list[dict]:
    repo = _require_repo(repo_id, me)
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
    me: Principal = Depends(get_principal),
) -> list[dict]:
    repo = _require_repo(repo_id, me)
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
def report_csv(repo_id: str, ts: str, branch: str = Query(...),
               me: Principal = Depends(get_principal)) -> Response:
    repo = _require_repo(repo_id, me)
    _require_branch(repo, branch)
    csv_text = storage.read_report_csv(repo_id, branch, ts)
    if csv_text is None:
        raise HTTPException(404, "report not found")
    _event(me, "csv_export")
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
    me: Principal = Depends(get_principal),
) -> dict:
    repo = _require_repo(repo_id, me)
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
    _event(me, "diff_view")
    return {"from": from_ts, "to": to_ts, "changed": changed, "added": added, "removed": removed}
