"""Scan pipeline: clone → extract → enrich → flag → snapshot.

A scan never throws to the caller: any stage failure degrades the affected data
(clone error → empty degraded snapshot; enrichment error → UNKNOWN) and is
recorded on the ScanStatus, so a run is never lost. One scan per (repo, branch)
at a time (asyncio lock = overlap guard) — each branch of a repo scans and
tracks state independently.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from . import flags, scan_context, scanlog, settings_store, storage, uploads
from .agents import apt_index, enrichment_agent, extraction_agent, image_inspect, source_apis
from .agents.clone import CloneError, shallow_clone
from .agents.image_inspect import ImageInspectError
from .config import get_settings
from .models import (
    Enrichment,
    Package,
    Repo,
    ScanIssue,
    ScanRunRecord,
    ScanState,
    ScanStatus,
    SnapshotSummary,
)


def _now() -> float:
    return time.monotonic()


def _since(t0: float) -> str:
    return f"{time.monotonic() - t0:.1f}"


def _secs(t0: float) -> float:
    return round(time.monotonic() - t0, 2)


def _extract_summary(packages: list[Package]) -> dict:
    by_eco: dict[str, int] = {}
    by_file: dict[str, int] = {}
    for p in packages:
        by_eco[p.ecosystem.value] = by_eco.get(p.ecosystem.value, 0) + 1
        by_file[p.source_file] = by_file.get(p.source_file, 0) + 1
    return {"by_ecosystem": by_eco, "by_source_file": by_file}


def _enrich_summary(items: list[Enrichment]) -> dict:
    by_verdict: dict[str, int] = {}
    for e in items:
        by_verdict[e.verdict.value] = by_verdict.get(e.verdict.value, 0) + 1
    return {"by_verdict": by_verdict}

# in-process scan state + per-(repo, branch) overlap lock
_status: dict[tuple[str, str], ScanStatus] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def get_status(repo_id: str, branch: str) -> ScanStatus:
    return _status.get(
        (repo_id, branch), ScanStatus(repo_id=repo_id, branch=branch, state=ScanState.idle)
    )


def clear_repo_state(repo_id: str, branch: str | None = None) -> None:
    """Drop in-memory scan status + lock for one branch, or every branch of
    `repo_id` when `branch` is omitted (repo deletion) — so a re-add (same id)
    starts clean rather than showing the old run's state."""
    if branch is not None:
        _status.pop((repo_id, branch), None)
        _locks.pop((repo_id, branch), None)
    else:
        for key in [k for k in _status if k[0] == repo_id]:
            _status.pop(key, None)
        for key in [k for k in _locks if k[0] == repo_id]:
            _locks.pop(key, None)


def _set(repo_id: str, branch: str, **kw) -> None:
    key = (repo_id, branch)
    st = _status.get(key) or ScanStatus(repo_id=repo_id, branch=branch)
    _status[key] = st.model_copy(update=kw)


def _fail(repo_id: str, branch: str, stage: str, message: str, *, fatal: bool = False) -> None:
    """Record a stage failure on the live status: append it to `issues` (so the
    UI can list every problem, not just the last one), mark the run degraded,
    and keep `message` as the most recent one-line summary."""
    key = (repo_id, branch)
    st = _status.get(key) or ScanStatus(repo_id=repo_id, branch=branch)
    _status[key] = st.model_copy(update={
        "issues": [*st.issues, ScanIssue(stage=stage, message=message, fatal=fatal)],
        "degraded": True,
        "message": message,
    })


def _lock(repo_id: str, branch: str) -> asyncio.Lock:
    return _locks.setdefault((repo_id, branch), asyncio.Lock())


def is_scanning(repo_id: str, branch: str) -> bool:
    return _lock(repo_id, branch).locked()


async def run_scan(
    repo: Repo,
    branch: str,
    *,
    use_extraction_agent: bool | None = None,
    trigger: str = "manual",
) -> ScanStatus:
    """Scan one branch. `trigger` says why it ran: scheduled, manual, initial
    (first scan after the repo was added) or upload."""
    lock = _lock(repo.id, branch)
    if lock.locked():
        return get_status(repo.id, branch)  # overlap guard: skip if already running

    async with lock:
        # everything below runs for the repo's workspace: its settings, AI
        # provider and tokens (see scan_context)
        scan_context.bind(scan_context.ScanContext(
            workspace_id=repo.workspace_id, created_by=repo.created_by))
        settings = get_settings()
        use_agent = (
            settings.use_extraction_agent
            if use_extraction_agent is None else use_extraction_agent
        )
        _set(repo.id, branch, state=ScanState.running, phase="clone",
             started_at=datetime.now(timezone.utc), message="", snapshot_ts=None,
             degraded=False, issues=[])

        ts = storage.new_snapshot_ts()
        log = scanlog.start(repo.id, branch)
        log.snapshot_ts = ts
        log.emit("scan", f"Scan started for {repo.id} ({repo.url} @ {branch})",
                 data={"release_date": str(repo.release_date), "model": settings.agent_model,
                       "extraction_agent": bool(use_agent)})

        degraded = False
        packages: list[Package] = []
        enrichments: list[Enrichment] = []
        images_to_inspect: list[str] = []
        detected_tag_vars: list[str] = []
        # source_file -> (distro, release) for OS packages; see enrichment_agent
        distro_by_source: dict[str, tuple[str, str]] = {}
        # per-Dockerfile base image + added apt repos, read while the clone exists
        docker_ctx = image_inspect.DockerfileContext()
        apt_indexes: dict[str, apt_index.AptIndex] = {}
        # how the optional AI research went; reported apart from `degraded`
        ai = enrichment_agent.AiOutcome()
        # facts for this scan's `scan_runs` row (see _record_run)
        started_at = datetime.now(timezone.utc)
        stage_seconds: dict[str, float] = {}
        counts: dict[str, int] = {}
        upstream_errors = source_apis.start_error_tally()

        def _phase(name: str) -> None:
            _set(repo.id, branch, phase=name)

        try:
            # 1. clone + 2. extract
            try:
                _phase("clone")
                t0 = _now()
                # `git clone` and the repo walks below are blocking calls on the
                # API's own event loop — run them in worker threads, or every
                # request (including the repo list the UI refetches right after
                # POST /repos) stalls until the scan's I/O finishes.
                if repo.source == "upload":
                    clone = await asyncio.to_thread(uploads.unpack, repo.id, branch)
                    done_msg = f"Unpacked uploaded zip for version {branch} in {_since(t0)}s"
                else:
                    clone = await asyncio.to_thread(
                        shallow_clone, repo.url, branch, repo.token_override
                    )
                    done_msg = f"Cloned {branch} in {_since(t0)}s"
                try:
                    stage_seconds["clone"] = _secs(t0)
                    log.emit("clone", done_msg, data={"path": str(clone.path)})
                    _phase("extract")
                    t1 = _now()
                    tag_vars = dict(repo.image_tags or {})
                    pkg_list = await extraction_agent.extract(
                        clone.path, use_agent=bool(use_agent), tag_vars=tag_vars
                    )
                    packages = pkg_list.packages
                    stage_seconds["extract"] = _secs(t1)
                    log.emit("extract",
                             f"Extracted {len(packages)} packages in {_since(t1)}s",
                             data=_extract_summary(packages))
                    # Compose files that take their tags from variables
                    # (`image: index.docker.io/${CORE_TAG}`) only yield images if
                    # the user supplied those tags. Report both sides so a missing
                    # value is visible in the trace instead of silently dropping
                    # the image.
                    docker_ctx = await asyncio.to_thread(image_inspect.dockerfile_context, clone.path)
                    needed = detected_tag_vars = await asyncio.to_thread(
                        image_inspect.detect_tag_vars, clone.path
                    )
                    if needed:
                        missing = [v for v in needed if not tag_vars.get(v)]
                        log.emit("extract",
                                 f"Compose image tags: {len(needed) - len(missing)}/"
                                 f"{len(needed)} variable(s) supplied"
                                 + (f"; missing {', '.join(missing)}" if missing else ""),
                                 level="warn" if missing else "info",
                                 data={"variables": needed, "missing": missing,
                                       "supplied": tag_vars})
                    # Discover which Docker image(s) to inventory while the clone
                    # is still on disk. A manual override (repo.inspect_image) wins;
                    # otherwise auto-detect from compose files + Dockerfiles.
                    if repo.inspect_image:
                        images_to_inspect = [repo.inspect_image]
                    else:
                        images_to_inspect = await asyncio.to_thread(
                            image_inspect.detect_images, clone.path, tag_vars
                        )
                    if images_to_inspect:
                        log.emit("extract",
                                 f"Detected {len(images_to_inspect)} Docker image(s) to inventory",
                                 data={"images": images_to_inspect})
                finally:
                    await asyncio.to_thread(clone.cleanup)
            except CloneError as e:
                stage_seconds.setdefault("clone", _secs(t0))
                degraded = True
                _fail(repo.id, branch, "clone", str(e))
                log.emit("clone", str(e), level="error")

            # Docker is a capability (image_inspect.inspect_mode). Without it, the
            # two Docker steps below are skipped and the scan isn't degraded.
            t_inspect = _now()
            docker_on, docker_why = await asyncio.to_thread(image_inspect.inspect_mode)

            # 2b. OS-package inventory of the detected Docker image(s) (dpkg parity)
            if images_to_inspect and not docker_on:
                log.emit("inspect",
                         f"Skipped the OS-package inventory of {len(images_to_inspect)} "
                         f"Docker image(s): {docker_why}. Base-image EOL and Dockerfile "
                         "apt packages are still checked.",
                         data={"images": images_to_inspect, "reason": docker_why})
            elif images_to_inspect:
                _phase("inspect")
                for image in images_to_inspect:
                    t_img = _now()
                    # surface the exact docker invocation in the trace (tool level)
                    log.emit("inspect",
                             f"Running docker to inventory image {image}",
                             level="tool",
                             data={"image": image,
                                   "command": image_inspect.inspect_command_str(image)})
                    try:
                        inventory = await image_inspect.list_image_packages(image)
                        packages = extraction_agent.dedup(packages + inventory)
                        names = [p.name for p in inventory]
                        log.emit("inspect",
                                 f"docker/dpkg-query returned {len(inventory)} OS packages "
                                 f"from {image} in {_since(t_img)}s",
                                 level="result",
                                 data={"image": image, "count": len(inventory),
                                       "packages": names})
                        # The image's own distribution decides how long those OS
                        # packages are supported — Ubuntu backports fixes for the
                        # life of the release, so upstream's schedule doesn't apply.
                        distro = await image_inspect.read_os_release(image)
                        if distro:
                            distro_by_source[f"image:{image}"] = distro
                            log.emit("inspect",
                                     f"{image} is built on {distro[0]} {distro[1]} — "
                                     "its OS packages take that support window",
                                     level="result",
                                     data={"image": image, "distro": list(distro)})
                    except ImageInspectError as e:
                        degraded = True
                        _fail(repo.id, branch, "inspect", str(e))
                        log.emit("inspect", str(e), level="error")

            # A Dockerfile's own apt installs get the same treatment via its
            # `FROM` distro base image (no inventory needed to know it).
            distro_by_source.update(image_inspect.distro_by_dockerfile(packages, docker_ctx.bases))

            # 2c. apt package indexes (Ubuntu archive + repos a Dockerfile adds):
            # fill in unpinned versions (`apt-get install curl`) and, during
            # enrichment, tell Ubuntu's own builds from third-party ones.
            apt_sources = {p.source_file for p in packages if p.ecosystem.value == "apt"}
            if apt_sources:
                _phase("inspect")
                t_idx = _now()
                try:
                    async with source_apis.make_client() as client:
                        apt_indexes = await apt_index.indexes_for_sources(
                            client, apt_sources, distro_by_source, docker_ctx.apt_repos)
                    packages, filled = apt_index.resolve_unpinned(packages, apt_indexes)
                    packages = extraction_agent.dedup(packages)
                    log.emit("inspect",
                             f"apt package indexes ready for {len(apt_indexes)}/{len(apt_sources)} "
                             f"source(s); resolved {len(filled)} unpinned version(s) "
                             f"in {_since(t_idx)}s",
                             level="result", data={"resolved": filled})
                except Exception as e:  # noqa: BLE001 - the docker fallback below still runs
                    log.emit("inspect", f"apt package indexes unavailable: {e}", level="warn")

            # 2d. Whatever is still unpinned (non-Ubuntu bases, index offline):
            # ask each base image's own apt what it would install.
            unpinned = sum(1 for p in packages if p.ecosystem.value == "apt" and not p.version)
            if unpinned and not docker_on:
                log.emit("inspect",
                         f"{unpinned} apt package(s) stay without a version: asking the base "
                         f"image needs Docker, and {docker_why}",
                         data={"unpinned": unpinned, "reason": docker_why})
            elif unpinned:
                _phase("inspect")
                t_res = _now()
                try:
                    def _emit(msg, *, level="result", data=None):
                        log.emit("inspect", msg, level=level, data=data or {})
                    before = sum(
                        1 for p in packages if p.ecosystem.value == "apt" and not p.version
                    )
                    packages = await image_inspect.reconcile_apt_versions(
                        packages, bases=docker_ctx.bases, emit=_emit)
                    packages = extraction_agent.dedup(packages)
                    after = sum(
                        1 for p in packages if p.ecosystem.value == "apt" and not p.version
                    )
                    if before:
                        log.emit("inspect",
                                 f"Resolved {before - after}/{before} unpinned apt "
                                 f"version(s) in {_since(t_res)}s", level="result")
                except Exception as e:  # noqa: BLE001 - never break the scan on this
                    # Same class of loss as a failed dpkg inventory (versions we
                    # can't determine), so it degrades the snapshot too — keeping
                    # SnapshotSummary.degraded and ScanStatus.degraded in step.
                    degraded = True
                    _fail(repo.id, branch, "inspect", f"apt version resolution degraded: {e}")
                    log.emit("inspect", f"apt version resolution degraded: {e}", level="error")
            stage_seconds["inspect"] = _secs(t_inspect)

            # 3. enrich
            if packages:
                _phase("enrich")
                t2 = _now()
                try:
                    enrichments = await enrichment_agent.enrich(
                        packages, distro_by_source=distro_by_source, apt_indexes=apt_indexes,
                        ai_outcome=ai,
                    )
                    log.emit("enrich",
                             f"Enriched {len(enrichments)} packages in {_since(t2)}s",
                             data=_enrich_summary(enrichments))
                except Exception as e:  # noqa: BLE001
                    degraded = True
                    _fail(repo.id, branch, "enrich", f"enrichment degraded: {e}")
                    log.emit("enrich", f"enrichment degraded: {e}", level="error")
                stage_seconds["enrich"] = _secs(t2)  # includes the AI pass
                if ai.batches:
                    stage_seconds["ai"] = ai.seconds
            elif settings_store.get_runtime_settings().get("use_enrichment_agent"):
                ai.status, ai.detail = "ok", "no packages to research"

            # 4. flag
            _phase("flag")
            t_flag = _now()
            enrich_by_key = {e.key: e for e in enrichments}
            flag_list = flags.evaluate_all(packages, enrich_by_key, repo.release_date,
                                           repo.ignored_packages)
            counts = flags.count_flags(flag_list)
            stage_seconds["flag"] = _secs(t_flag)
            log.emit("flag", f"Flagged {len(flag_list)} packages", data={"counts": counts})

            # 5. snapshot (each payload is one DB transaction; the snapshot is
            #    only listed once the index entry below commits)
            _phase("snapshot")
            t_snap = _now()
            storage.write_packages(repo.id, branch, ts, packages)
            storage.write_enrichment(repo.id, branch, ts, enrichments)
            storage.write_flags(repo.id, branch, ts, flag_list)
            summary = SnapshotSummary(
                ts=ts, package_count=len(packages), flag_counts=counts, degraded=degraded,
                ai_status=ai.status, ai_detail=ai.detail or None,
            )
            storage.upsert_index_entry(repo.id, branch, summary)
            stage_seconds["snapshot"] = _secs(t_snap)
            log.emit("snapshot", f"Wrote snapshot {ts}"
                     + (" (degraded)" if degraded else ""),
                     data={"stored": ["packages", "enrichment", "flags"]})

            # touch repo.last_scan_by_branch + record the compose image-tag
            # variables this scan saw, so the UI can prompt for exactly those tags
            fresh = storage.get_repo(repo.id)
            if fresh:
                fresh.last_scan_by_branch[branch] = datetime.now(timezone.utc)
                if detected_tag_vars:
                    fresh.detected_tag_vars = detected_tag_vars
                storage.upsert_repo(fresh)

            log.state = ScanState.done
            log.emit("scan", "Scan complete")
            _set(repo.id, branch, state=ScanState.done, phase="done",
                 finished_at=datetime.now(timezone.utc), snapshot_ts=ts)
        except Exception as e:  # noqa: BLE001 - last-resort guard
            log.state = ScanState.error
            log.emit("scan", f"Scan failed: {e}", level="error")
            _fail(repo.id, branch, "scan", str(e), fatal=True)
            _set(repo.id, branch, state=ScanState.error, phase="error",
                 finished_at=datetime.now(timezone.utc))
        finally:
            # persist the trace alongside the snapshot for later viewing
            try:
                storage.write_scan_log(repo.id, branch, ts, log.to_model())
            except Exception:  # noqa: BLE001
                pass
            _record_run(
                repo, branch, ts, trigger=trigger, started_at=started_at,
                stage_seconds=stage_seconds, packages=packages, flag_counts=counts,
                ai=ai, upstream_errors=upstream_errors,
            )

        return get_status(repo.id, branch)


def _record_run(
    repo: Repo,
    branch: str,
    ts: str,
    *,
    trigger: str,
    started_at: datetime,
    stage_seconds: dict[str, float],
    packages: list[Package],
    flag_counts: dict[str, int],
    ai: enrichment_agent.AiOutcome,
    upstream_errors: dict[str, int],
) -> None:
    """Write this scan's `scan_runs` fact row. Analytics must never break a
    scan, so a failure here is only logged."""
    try:
        st = get_status(repo.id, branch)
        outcome = "error" if st.state == ScanState.error else ("degraded" if st.degraded else "done")
        tokens = ai.tokens or {}
        ran_ai = ai.batches > 0
        storage.write_scan_run(ScanRunRecord(
            repo_id=repo.id,
            workspace_id=repo.workspace_id,
            branch=branch,
            snapshot_ts=None if outcome == "error" else ts,
            triggered_by=trigger,
            started_at=started_at,
            finished_at=st.finished_at or datetime.now(timezone.utc),
            outcome=outcome,
            stage_seconds=stage_seconds,
            issue_stages=list(dict.fromkeys(i.stage for i in st.issues)),
            first_error=st.issues[0].message[:500] if st.issues else None,
            package_count=len(packages),
            package_counts=_extract_summary(packages)["by_ecosystem"],
            flag_counts=dict(flag_counts),
            ai_status=ai.status,
            ai_detail=ai.detail or None,
            ai_provider=ai.provider if ran_ai else None,
            ai_model=ai.model if ran_ai else None,
            ai_batches=ai.batches,
            ai_failed_batches=ai.failed_batches,
            ai_tokens_in=sum(int(tokens.get(k) or 0) for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
            ai_tokens_out=int(tokens.get("output_tokens") or 0),
            ai_searches=int(tokens.get("web_search_requests") or 0),
            # None = not estimated (a third-party model we don't price), never a guess
            ai_cost_usd=(tokens.get("cost_usd") if ran_ai and not tokens.get("cost_unknown")
                         else None),
            upstream_errors=dict(upstream_errors),
        ))
    except Exception:  # noqa: BLE001 - analytics must never break a scan
        logging.getLogger("packages-tracker.pipeline").exception(
            "could not record the scan run for %s @ %s", repo.id, branch)
