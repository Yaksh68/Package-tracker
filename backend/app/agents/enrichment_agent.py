"""Agent 2 — EOL/EOS research.

Every scan looks everything up fresh: we gather structured signals via
source_apis (endoflife.date / PyPI / npm / GitHub repo + SECURITY.md) and synthesize a verdict.
Packages that resolve cleanly from structured sources are decided
deterministically (no AI cost); genuinely ambiguous ones are handed to the AI
agent (WebSearch/WebFetch) in `_ai_pass`.

There is deliberately **no result cache** — a scan always reflects today's
upstream state, so a package that went EOL / got a new release since the last
run shows up immediately rather than waiting out a TTL. Lookups are deduped by
`ecosystem:name:version` within a scan, so a library pinned in several manifests
is still only fetched once.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from pydantic import ValidationError

from .. import scan_context, scanlog, settings_store, storage
from ..config import get_settings
from ..models import (
    LOCAL_WORKSPACE_ID,
    AiVerdict,
    AiVerdictBatch,
    Confidence,
    Ecosystem,
    Enrichment,
    Package,
    Source,
    Verdict,
)
from ..ai import citations
from ..ai import config as ai_config
from ..ai.errors import AiError, redact_keys
from . import apt_index, source_apis
from .transport import run_text_agent, sum_tokens


@dataclass
class AiOutcome:
    """How the AI research pass went for one scan. It's reported separately
    from the structured results, which an AI problem never touches: a failed
    or skipped AI pass leaves every endoflife.date / registry / GitHub verdict
    in place, and doesn't make the snapshot "degraded".

    status: off (toggle off) | ok | partial (some batches failed) | failed |
    quota (rate limit or quota reached) | no_key (no provider configured;
    used once API keys arrive)."""
    status: str = "off"
    detail: str = ""
    candidates: int = 0
    batches: int = 0
    failed_batches: int = 0
    skipped_batches: int = 0
    seconds: float = 0.0
    tokens: dict = field(default_factory=dict)
    provider: str | None = None  # AiConfig.label, e.g. "anthropic", "openai-compatible:groq"
    model: str | None = None
    reused: int = 0  # findings taken from recent scans instead of researched again


def _stale(last: date | None, years: int) -> bool:
    if not last:
        return False
    return (date.today() - last).days > years * 365


# A Debian/Ubuntu revision suffix — `-0ubuntu2.1`, `-1ubuntu0.15`, `-2build1`,
# `-3.2ubuntu0.3`, `1:2.39.3-9ubuntu6.5`, `~24.04.1`, `-1~deb12u2`. Its presence
# means the distribution rebuilt and now maintains this package: it backports
# fixes on the *distro's* schedule, so upstream's release train says nothing
# about how long it is supported.
_DISTRO_REVISION = re.compile(r"(?:ubuntu|build|deb|~\d+\.\d+)", re.IGNORECASE)


def is_distro_build(pkg: Package) -> bool:
    """True for an apt package carrying a distribution revision — the case where
    an upstream product cycle must NOT be applied.

    The counter-example is `mongodb-org 8.3.4`, installed from MongoDB's own apt
    repo with a pristine upstream version: no distro rebuilt it, so upstream's
    schedule is exactly right."""
    return (
        pkg.ecosystem is Ecosystem.apt
        and bool(pkg.version)
        and bool(_DISTRO_REVISION.search(pkg.version))
    )


async def _apt_hit(
    client: httpx.AsyncClient,
    pkg: Package,
    index: apt_index.AptIndex,
    distro: tuple[str, str] | None,
) -> apt_index.Hit | None:
    """Where this apt package comes from, per the indexes its image's apt sees:
    the exact version's index entry, else (Ubuntu only) Launchpad, which also
    knows versions a later update has since superseded. Version-less: the
    candidate apt would install."""
    if not pkg.version:
        return index.candidate(pkg.name)
    hit = index.published(pkg.name, pkg.version)
    # Launchpad is one call per package — only spent where the version string
    # alone (`is_distro_build`) can't already tell us Ubuntu built it.
    if hit is None and index.series and distro and distro[0] == "ubuntu" and not is_distro_build(pkg):
        hit = await apt_index.launchpad_published(
            client, index.series, pkg.name, pkg.version, label=f"ubuntu {distro[1]}")
    return hit


async def _enrich_one(
    client: httpx.AsyncClient,
    pkg: Package,
    *,
    today: date,
    distro: tuple[str, str] | None = None,
    index: apt_index.AptIndex | None = None,
) -> Enrichment:
    e = Enrichment(name=pkg.name, version=pkg.version, ecosystem=pkg.ecosystem)
    sources: list[Source] = []
    conf = Confidence.low

    hit = None
    if pkg.ecosystem is Ecosystem.apt and index is not None:
        hit = await _apt_hit(client, pkg, index, distro)
        cand = index.candidate(pkg.name)
        if cand:
            # newest the image's repos offer — for an Ubuntu package that is the
            # latest build within the release, not upstream's latest
            e.latest_version = e.latest_stable_version = cand.version

    # --- distro-maintained OS package ---------------------------------------
    # Its support window is the distribution's, not the upstream project's, and
    # the two are years apart. Looking the *name* up on endoflife.date is what
    # reported `perl 5.38.2-3.2ubuntu0.3` as ALREADY_EOL while Ubuntu supports
    # it until 2029. Decided by the archive when we have it (an index hit from a
    # third-party repo is NOT distro-maintained, whatever its version says), else
    # by the distro revision in the version string.
    if hit is not None and hit.is_ubuntu or (hit is None and is_distro_build(pkg)):
        conf = await _apply_distro_window(client, e, sources, distro, today=today, hit=hit)

    # --- vendor lifecycle page (e.g. Microsoft's ODBC driver) ---------------
    elif (vendor := await source_apis.query_vendor_lifecycle(client, pkg.name)).get("matched"):
        e.eol_date = vendor["eol_date"]
        e.verdict = Verdict.eol if e.eol_date <= today else Verdict.supported
        conf = Confidence.high
        sources.append(Source(url=vendor["url"], note=f"{vendor['product']} lifecycle"))
        if hit is not None:
            sources.append(Source(url=_repo_url(hit), note=f"{hit.label} {hit.suite}/{hit.component}"))
        e.summary = (f"{vendor['product']}: mainstream support until "
                     f"{e.eol_date.isoformat()} (vendor lifecycle).")

    # --- endoflife.date (products: base images, apt base, major libs) --------
    else:
        eol = await source_apis.query_endoflife(client, pkg.name, pkg.version)
        if eol.get("matched"):
            # Record the match even when there's no EOL date yet (e.g. React 19 —
            # a tracked product whose current major has no announced end-of-life):
            # keep the citation and the latest version.
            e.latest_version = eol.get("latest") or e.latest_version
            e.latest_stable_version = eol.get("latest_stable") or e.latest_stable_version
            sources.append(Source(url=eol["url"], note=f"endoflife.date cycle {eol.get('cycle')}"))
            if eol.get("eol_date"):
                e.eol_date = eol.get("eol_date")
                conf = Confidence.high
                e.verdict = Verdict.eol if (eol.get("is_eol") or e.eol_date <= today) else Verdict.supported
            else:
                conf = max(conf, Confidence.medium, key=_conf_rank)
                if eol.get("is_eol"):
                    e.verdict = Verdict.eol
                elif e.verdict == Verdict.unknown:
                    e.verdict = Verdict.supported

    # --- registry metadata ---------------------------------------------------
    if pkg.ecosystem == Ecosystem.pypi:
        reg = await source_apis.query_pypi(client, pkg.name)
        if reg.get("found"):
            e.latest_version = e.latest_version or reg.get("latest_version")
            e.latest_stable_version = e.latest_stable_version or reg.get("latest_stable_version")
            e.last_release = reg.get("last_release")
            sources.append(Source(url=reg["url"], note="PyPI"))
            conf = max(conf, Confidence.medium, key=_conf_rank)
            if reg.get("inactive") or reg.get("yanked"):
                e.verdict = Verdict.deprecated
            elif e.verdict == Verdict.unknown:
                e.verdict = Verdict.supported
            if reg.get("repo_url"):
                await _apply_github(client, e, reg["repo_url"], sources)

    elif pkg.ecosystem == Ecosystem.npm:
        reg = await source_apis.query_npm(client, pkg.name)
        if reg.get("found"):
            e.latest_version = e.latest_version or reg.get("latest_version")
            e.latest_stable_version = e.latest_stable_version or reg.get("latest_stable_version")
            e.last_release = reg.get("last_release")
            sources.append(Source(url=reg["url"], note="npm"))
            conf = max(conf, Confidence.medium, key=_conf_rank)
            if reg.get("deprecated"):
                e.verdict = Verdict.deprecated
                if isinstance(reg["deprecated"], str):
                    e.summary = reg["deprecated"][:280]
            elif e.verdict == Verdict.unknown:
                e.verdict = Verdict.supported
            if reg.get("repo_url"):
                await _apply_github(client, e, reg["repo_url"], sources)

    if e.security_supported is not None:
        conf = max(conf, Confidence.medium, key=_conf_rank)

    e.confidence = conf
    e.sources = sources
    # EOS is end-of-**security**-support — the same milestone endoflife.date
    # publishes as "end of life", so the two fields carry one date. The
    # end-of-*active*-support date (`eoasFrom`) is deliberately not tracked: it
    # only marks the start of security-only maintenance, and treating it as
    # "support ended" reported still-supported packages as at risk.
    e.eos_date = e.eol_date
    if not e.summary:
        e.summary = _summarize(e, today)

    scanlog.emit(
        "source",
        f"{pkg.ecosystem.value}:{pkg.name} → {e.verdict.value}"
        + (f" (EOL {e.eol_date.isoformat()})" if e.eol_date else ""),
        data={
            "package": pkg.name,
            "version": pkg.version,
            "queried": [s.note or s.url for s in sources] or ["no source matched"],
            "verdict": e.verdict.value,
            "confidence": e.confidence.value,
            "eol_date": e.eol_date.isoformat() if e.eol_date else None,
        },
    )
    return e


def _repo_url(hit: apt_index.Hit) -> str:
    if hit.is_ubuntu:
        series = hit.suite.split("-")[0]
        return f"https://launchpad.net/ubuntu/{series}/+package/{hit.name}"
    return f"https://{hit.label}"


async def _apply_distro_window(
    client: httpx.AsyncClient,
    e: Enrichment,
    sources: list[Source],
    distro: tuple[str, str] | None,
    *,
    today: date,
    hit: apt_index.Hit | None = None,
) -> Confidence:
    """Give a distro-maintained OS package its distribution's support window
    (`("ubuntu", "24.04")` → EOL 2029-05-31, cited to endoflife.date/ubuntu).

    Without a known distro nothing is asserted — the package stays unknown,
    which is the honest answer and still better than an upstream date that
    doesn't apply."""
    if not distro:
        e.summary = (
            "Distribution-maintained OS package; the image's support window "
            "could not be determined."
        )
        return Confidence.low

    os_id, os_version = distro
    cyc = await source_apis.query_endoflife(client, os_id, os_version)
    if not cyc.get("matched") or not cyc.get("eol_date"):
        e.summary = (
            f"Maintained by {os_id} {os_version}, which publishes no support "
            "window we can resolve."
        )
        return Confidence.low

    e.eol_date = cyc["eol_date"]
    e.verdict = (
        Verdict.eol if (cyc.get("is_eol") or e.eol_date <= today) else Verdict.supported
    )
    sources.append(Source(
        url=cyc["url"],
        note=f"endoflife.date {os_id} {cyc.get('cycle')} — distribution support window",
    ))
    e.summary = (
        f"Maintained by {os_id} {os_version}, which backports fixes for the "
        f"life of the release: supported until {e.eol_date.isoformat()}, "
        "independent of the upstream project's own schedule."
    )
    if hit is not None:
        via = f" (provided by {hit.provided_by})" if hit.provided_by else ""
        sources.append(Source(
            url=_repo_url(hit),
            note=f"Ubuntu {os_version} archive: {hit.name} {hit.version} in {hit.suite}/{hit.component}{via}",
        ))
        if hit.provided_by:
            e.summary += f" Installed as {hit.provided_by}, which provides {e.name}."
        if hit.component in ("universe", "multiverse"):
            # Canonical's 5-year guarantee covers main/restricted; universe gets
            # community best-effort fixes (10 years only with Ubuntu Pro).
            e.summary += (f" It is in {hit.component}: community-maintained — Canonical "
                          "guarantees security fixes only with Ubuntu Pro.")
    return Confidence.high


def _conf_rank(c: Confidence) -> int:
    return {Confidence.low: 0, Confidence.medium: 1, Confidence.high: 2}[c]


async def _apply_github(
    client: httpx.AsyncClient, e: Enrichment, repo_url: str, sources: list[Source]
) -> None:
    gh = await source_apis.query_github_repo(client, repo_url)
    # Archived because it moved (e.g. into a monorepo): judge the new home
    # instead. One hop only — no chasing chains of moves.
    new_slug = source_apis.github_slug(gh.get("moved_to"))
    if gh.get("found") and new_slug and new_slug.lower() != gh["slug"].lower():
        sources.append(Source(url=gh["url"], note=f"GitHub (archived; moved to {gh['moved_to']})"))
        moved = await source_apis.query_github_repo(client, new_slug)
        if moved.get("found"):
            gh = moved
    if gh.get("found"):
        e.archived = gh.get("archived")
        # Releases published after the repo's last push were built from
        # somewhere else: the project lives on, only this copy is frozen.
        if e.archived and e.last_release and gh.get("pushed_at") and e.last_release > gh["pushed_at"]:
            e.archived = False
            scanlog.emit(
                "source",
                f"{e.name}: {gh['url']} is archived, but {e.name} was released "
                f"{e.last_release.isoformat()}, after its last push — not treated as archived",
            )
        if gh.get("pushed_at") and not e.last_release:
            e.last_release = gh.get("pushed_at")
        if gh.get("url"):
            sources.append(Source(url=gh["url"], note="GitHub"))
        if e.archived and e.verdict in (Verdict.unknown, Verdict.supported):
            e.verdict = Verdict.deprecated
    elif gh.get("status") in (403, 429):
        scanlog.emit(
            "source",
            f"GitHub API rate-limited for {repo_url} — archived check skipped "
            "(set GITHUB_API_TOKEN to raise the limit)",
            level="warn",
        )

    # SECURITY.md comes from raw.githubusercontent.com, which has no API quota —
    # so it is read even when the repo-metadata call above was rate-limited.
    slug = gh.get("slug") or source_apis.github_slug(repo_url)
    if slug:
        pol = await source_apis.query_github_security_policy(
            client, slug, e.version, ref=gh.get("default_branch") or "HEAD",
        )
        _apply_security_policy(e, pol, sources)


def _apply_security_policy(e: Enrichment, pol: dict, sources: list[Source]) -> None:
    """Fold the upstream SECURITY.md verdict in. An explicit ❌ for this release
    line means it is no longer supported — EOL by this tracker's definition —
    even when the registry still looks healthy (gunicorn 24.x on PyPI, say)."""
    if not pol.get("found") or pol.get("supported") is None:
        return
    e.security_supported = pol["supported"]
    e.security_policy_row = pol.get("row")
    mark = "supported" if pol["supported"] else "unsupported"
    sources.append(Source(
        url=pol["url"],
        note=f"GitHub SECURITY.md: {e.version} (row '{pol.get('row')}') {mark}",
    ))
    if not pol["supported"]:
        e.verdict = Verdict.eol
        e.summary = (
            f"Upstream SECURITY.md lists '{pol.get('row')}' as unsupported: "
            f"{e.version} is end of life."
        )
    elif e.verdict == Verdict.unknown:
        e.verdict = Verdict.supported


def _summarize(e: Enrichment, today: date) -> str:
    if e.verdict == Verdict.eol and e.eol_date:
        return f"End-of-life since {e.eol_date.isoformat()}."
    if e.eol_date:
        return f"Supported; end-of-life scheduled {e.eol_date.isoformat()}."
    if e.verdict == Verdict.deprecated:
        return "Marked deprecated by its registry/source."
    if e.verdict == Verdict.supported:
        return "Active / supported per registry metadata."
    return "No authoritative EOL signal found."


async def enrich(
    packages: list[Package],
    *,
    today: date | None = None,
    distro_by_source: dict[str, tuple[str, str]] | None = None,
    apt_indexes: dict[str, apt_index.AptIndex] | None = None,
    ai_outcome: AiOutcome | None = None,
) -> list[Enrichment]:
    """Enrich a list of packages against today's upstream state (no caching).

    Deduped by `ecosystem:name:version`, so the same pinned library appearing in
    several manifests costs one lookup per scan.

    `distro_by_source` maps a `source_file` to the distribution behind it
    (`{"image:ubuntu:24.04": ("ubuntu", "24.04")}`) — the support window applied
    to that source's distro-maintained OS packages.

    `ai_outcome`, when given, is filled in with how the AI pass went. The AI
    pass is optional: whatever it does, including raising, the structured
    results are returned."""
    outcome = ai_outcome if ai_outcome is not None else AiOutcome()
    today = today or date.today()
    distro_by_source = distro_by_source or {}

    by_key: dict[str, Package] = {}
    for p in packages:
        by_key.setdefault(p.key, p)

    scanlog.emit(
        "enrich",
        f"Looking up {len(by_key)} unique package(s) of {len(packages)}",
        data={"lookups": len(by_key), "packages": len(packages)},
    )

    results: dict[str, Enrichment] = {}
    if by_key:
        async with source_apis.make_client() as client:
            for pkg in by_key.values():
                try:
                    e = await _enrich_one(
                        client, pkg, today=today,
                        distro=distro_by_source.get(pkg.source_file),
                        index=(apt_indexes or {}).get(pkg.source_file),
                    )
                except Exception as ex:  # noqa: BLE001 - degrade, never fail scan
                    e = Enrichment(
                        name=pkg.name, version=pkg.version, ecosystem=pkg.ecosystem,
                        verdict=Verdict.unknown, confidence=Confidence.low,
                        summary=f"enrichment error: {ex}",
                    )
                results[pkg.key] = e

    # --- AI research second pass ---------------------------------------------
    # Ambiguous packages only (see `_needs_ai`). With no cache, this runs on
    # every scan when the toggle is on. It must never cost the scan its
    # structured results: before this guard, an exception from the AI (CLI
    # missing, a batch timing out) escaped enrich() and the pipeline flagged
    # every package UNKNOWN.
    if settings_store.get_runtime_settings().get("use_enrichment_agent"):
        cfg, why = ai_config.resolve()
        if cfg is None:
            # AI is on but nothing can run it (no key, no provider): skip it.
            outcome.status, outcome.detail = "no_key", why or "no AI provider is configured"
            scanlog.emit("enrich", f"AI research skipped: {outcome.detail}", level="warn")
        else:
            outcome.provider, outcome.model = cfg.label, cfg.model
            try:
                await _ai_pass(list(results.values()), today=today, outcome=outcome)
            except Exception as ex:  # noqa: BLE001 - AI is optional; keep the structured verdicts
                outcome.status = "failed"
                outcome.detail = _error_text(ex)
                scanlog.emit("agent", f"AI research failed, structured verdicts kept: {outcome.detail}",
                             level="warn")
    else:
        outcome.status = "off"

    # preserve input order (first occurrence per key)
    return [results[k] for k in by_key]


# --------------------------------------------------------------------------- #
# AI research second pass                                                      #
# --------------------------------------------------------------------------- #
def _has_endoflife_source(e: Enrichment) -> bool:
    return any("endoflife" in (s.note + s.url).lower() for s in e.sources)


def _needs_ai(e: Enrichment, *, stale_years: int) -> bool:
    """Only ambiguous packages: unknown verdict, low confidence, or a stale lib
    with no authoritative EOL signal. High-confidence structured hits are skipped."""
    # OS packages are excluded: their support window is the base image's cycle
    # (tracked via endoflife.date), and image inventories would flood the AI
    # pass with hundreds of low-value lookups.
    if e.ecosystem == Ecosystem.apt:
        return False
    # The maintainers' own SECURITY.md already answered it.
    if e.security_supported is False:
        return False
    if e.verdict == Verdict.unknown:
        return True
    if e.confidence == Confidence.low:
        return True
    if not e.eol_date and not _has_endoflife_source(e) and e.last_release:
        from datetime import date
        if (date.today() - e.last_release).days > stale_years * 365:
            return True
    return False


def _chunk(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


_AI_SYSTEM = (
    "You are an open-source dependency EOL/EOS researcher. For each package you are "
    "given the structured signals already collected. Use web_search / web_fetch to "
    "find evidence of deprecation, project archival, or a superseding/successor "
    "library, and decide a verdict. RULES: (1) Return ONLY JSON, no prose. "
    "(2) Do NOT output any dates — dates are handled elsewhere. (3) Every non-"
    "'supported' verdict and every successor MUST be backed by a citation URL in "
    "'sources'. (4) If you find no evidence of deprecation/EOL, verdict='supported'. "
    "(5) Be conservative: a wrong 'eol' is worse than 'supported'."
)

_AI_PROMPT_TMPL = """\
Research the EOL/deprecation status of these packages. Use at most {max_searches} \
web searches in total. Return ONLY this JSON shape:
{{"verdicts": [{{"name": "...", "ecosystem": "pypi|npm|apt|docker-image|other", \
"verdict": "supported|deprecated|eol|unknown", "successor": "name or null", \
"confidence": "high|medium|low", "summary": "one sentence", \
"sources": [{{"url": "...", "note": "..."}}]}}]}}

Packages (with signals already gathered):
{packages}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in agent output")
    return json.loads(text[start : end + 1])


def _signal_line(e: Enrichment) -> dict:
    return {
        "name": e.name,
        "ecosystem": e.ecosystem.value,
        "version": e.version,
        "latest_version": e.latest_version,
        "latest_stable_version": e.latest_stable_version,
        "last_release": e.last_release.isoformat() if e.last_release else None,
        "archived": e.archived,
        "security_supported": e.security_supported,
        "current_verdict": e.verdict.value,
        "known_sources": [s.note or s.url for s in e.sources],
    }


def _merge_ai(e: Enrichment, v) -> None:
    """Apply an AiVerdict to a candidate. Never touches dates (structured-only)."""
    # Never let web research overrule an explicit upstream "unsupported".
    if v.verdict and v.verdict != Verdict.unknown and e.security_supported is not False:
        e.verdict = v.verdict
    if v.successor:
        e.successor = v.successor
    if v.summary:
        e.summary = v.summary
    e.confidence = max(e.confidence, v.confidence, key=_conf_rank)
    urls = {s.url for s in e.sources}
    for s in v.sources:
        if s.url and s.url not in urls:
            s.origin = "web"  # tag AI-found citations as web research
            e.sources.append(s)
            urls.add(s.url)


def _error_text(ex: BaseException) -> str:
    text = redact_keys(f"{type(ex).__name__}: {ex}" if str(ex) else type(ex).__name__)
    return text if len(text) <= 300 else text[:300] + "…"


def _exception_kind(ex: BaseException) -> str:
    # The providers raise typed errors; the CLI raises AgentError with a message.
    return ex.kind if isinstance(ex, AiError) else _classify(_error_text(ex))


# What a failed batch's error says about the next one. Rejected credentials,
# an exhausted quota or a missing CLI fail every remaining batch the same way,
# so the pass stops instead of repeating the call.
_QUOTA_HINTS = ("429", "rate limit", "rate_limit", "quota", "usage limit", "credit balance",
                "too many requests", "insufficient credit")
_AUTH_HINTS = ("401", "403", "authentication", "unauthorized", "invalid api key",
               "invalid x-api-key", "not logged in", "/login", "permission denied", "forbidden")
_CONFIG_HINTS = ("could not find the `claude` cli", "claude_cli_path", "filenotfounderror",
                 "no such file or directory", "cannot find the file")
_STOP_KINDS = {"auth", "quota", "config"}
_KIND_TEXT = {
    "auth": "authentication failed — check the API key or the Claude CLI login",
    "quota": "rate limit or quota reached",
    "config": "the AI isn't set up on this server",
    "timeout": "timed out",
    "invalid": "the AI's answer wasn't valid JSON",
    "error": "the AI call failed",
}


def _classify(message: str) -> str:
    m = message.lower()
    if m.startswith("invalid output"):
        return "invalid"
    if any(h in m for h in _QUOTA_HINTS):
        return "quota"
    if any(h in m for h in _AUTH_HINTS):
        return "auth"
    if any(h in m for h in _CONFIG_HINTS):
        return "config"
    if "timed out" in m or "timeout" in m:
        return "timeout"
    return "error"


def _outcome_status(total: int, failures: list[str], skipped: int, stopped: str | None) -> tuple[str, str]:
    if not failures:
        return "ok", f"{total} batch(es) researched"
    lost = len(failures) + skipped
    if stopped == "quota":
        status = "quota"
    elif lost >= total:
        status = "failed"
    else:
        status = "partial"
    detail = f"{len(failures)} of {total} batch(es) failed: {_KIND_TEXT[stopped or failures[-1]]}"
    if skipped:
        detail += f"; {skipped} skipped"
    return status, detail


async def _run_ai_batch(
    batch: list[Enrichment], *, max_searches: int, errors: list[tuple[str, str]] | None = None,
) -> tuple[AiVerdictBatch | None, dict]:
    """Return the parsed verdicts (or None) and the tokens this batch spent.
    Never raises: why a batch failed is appended to `errors` as (kind, message).

    When the provider reports which URLs its searches returned, the citation
    check drops sources the model never saw (see ai/citations.py).

    A retry after malformed output is charged into the same total: the tokens
    were spent whether or not the second attempt parsed."""
    errors = errors if errors is not None else []
    prompt = _AI_PROMPT_TMPL.format(
        max_searches=max_searches,
        packages=json.dumps([_signal_line(e) for e in batch], indent=2),
    )
    spent: dict = {}
    for _ in range(2):  # initial + one retry on malformed output
        try:
            res = await run_text_agent(
                prompt,
                allowed_tools=["WebSearch", "WebFetch"],
                append_system=_AI_SYSTEM,
                timeout=420.0,
            )
        except Exception as ex:  # noqa: BLE001 - the caller keeps the structured verdicts
            # No retry: a timeout or a missing CLI won't fix itself, and a
            # retried timeout would double the wait.
            errors.append((_exception_kind(ex), _error_text(ex)))
            return None, spent
        sum_tokens(spent, res.tokens())
        if res.is_error:
            message = redact_keys(res.error_message or "agent error")
            errors.append((_classify(message), message))
            scanlog.emit("agent", f"AI enrichment batch error: {message}", level="warn")
            if errors[-1][0] in _STOP_KINDS:
                return None, spent  # a rejected key or exhausted quota fails again
            continue
        try:
            parsed = AiVerdictBatch(**_extract_json(res.text))
        except Exception as ex:  # noqa: BLE001 - malformed → retry then degrade
            errors.append(("invalid", f"invalid output: {ex}"))
            scanlog.emit("agent", f"AI batch output invalid ({ex}); retrying", level="warn")
            continue
        # Every problem verdict needs a citation, from every provider. When the
        # provider shows what its searches returned, the citation must also be
        # one of those pages.
        parsed, dropped = citations.enforce(parsed, getattr(res, "seen_urls", None))
        if dropped["citations"] or dropped["verdicts"] or dropped["successors"]:
            scanlog.emit(
                "agent",
                f"Citation check: dropped {dropped['citations']} unseen citation(s); "
                f"{len(dropped['verdicts'])} uncited verdict(s) kept their structured result, "
                f"{len(dropped['successors'])} uncited successor(s) cleared",
                level="warn", data=dropped,
            )
        return parsed, spent
    return None, spent


def _reuse_recent(candidates: list[Enrichment]) -> int:
    """AI verdict reuse: apply the AI's answer for the same ecosystem, name and
    version from this workspace's scans of the last `ai_reuse_days` days,
    instead of researching it again. Structured lookups stay fresh: only the
    AI's part is reused, merged as if it had just answered. Returns how many."""
    days = int(settings_store.get_runtime_settings().get("ai_reuse_days") or 0)
    if days <= 0:
        return 0
    since = datetime.now(timezone.utc) - timedelta(days=days)
    workspace = scan_context.workspace_id() or LOCAL_WORKSPACE_ID
    found: dict[str, tuple[str, dict]] = {}
    for ts, data in storage.recent_ai_results(workspace, [e.name for e in candidates], since):
        key = f"{data.get('ecosystem')}:{str(data.get('name', '')).lower()}:{data.get('version') or ''}"
        found.setdefault(key, (ts, data))  # newest first
    reused = 0
    for e in candidates:
        if (hit := found.get(e.key)) is None:
            continue
        ts, data = hit
        try:
            v = AiVerdict.model_validate(data["ai_result"])
        except ValidationError:
            continue
        _merge_ai(e, v)
        e.ai_result, e.ai_checked, e.ai_reused_from = data["ai_result"], True, ts
        reused += 1
    if reused:
        scanlog.emit("enrich", f"AI research: reused {reused} finding(s) from this workspace's scans "
                               f"of the last {days} day(s) instead of researching them again",
                     data={"reused": [e.key for e in candidates if e.ai_reused_from]})
    return reused


async def _ai_pass(
    pool: list[Enrichment], *, today, outcome: AiOutcome | None = None,
) -> list[Enrichment]:
    """Run the AI research agent over the ambiguous, not-yet-AI-checked entries in
    `pool`, and return those candidates. Only candidates in a batch that came
    back with verdicts are marked `ai_checked`. A failed batch never raises:
    it's recorded on `outcome`, and its packages keep their structured
    verdicts."""
    outcome = outcome if outcome is not None else AiOutcome()
    settings = get_settings()
    candidates = [
        e for e in pool
        if _needs_ai(e, stale_years=settings.stale_years) and not e.ai_checked
    ]
    outcome.candidates = len(candidates)
    if not candidates:
        outcome.status, outcome.detail = "ok", "no ambiguous packages"
        scanlog.emit("enrich", "AI enrichment: 0 new ambiguous packages, skipped")
        return []

    outcome.reused = _reuse_recent(candidates)
    to_research = [e for e in candidates if not e.ai_reused_from]
    if not to_research:
        outcome.status, outcome.detail = "ok", f"reused {outcome.reused} recent finding(s)"
        return candidates

    batches = _chunk(to_research, settings.enrichment_batch_size)
    scanlog.emit(
        "enrich",
        f"AI enrichment: {len(to_research)} ambiguous packages → {len(batches)} batch(es)",
        data={"candidates": [f"{e.ecosystem.value}:{e.name}" for e in to_research]},
    )

    t0 = time.monotonic()
    totals: dict = {}
    failures: list[str] = []  # the error kind of each failed batch
    attempted = 0
    stopped: str | None = None
    for i, batch in enumerate(batches, start=1):
        attempted += 1
        errors: list[tuple[str, str]] = []
        result, spent = await _run_ai_batch(
            batch, max_searches=settings.web_search_max_uses, errors=errors,
        )
        sum_tokens(totals, spent)
        scanlog.emit(
            "agent",
            f"Batch tokens: {spent.get('total_tokens', 0):,} "
            f"({spent.get('input_tokens', 0):,} in / {spent.get('output_tokens', 0):,} out) "
            f"over {len(batch)} package(s)",
            data=spent,
        )
        if result is None:
            kind, message = errors[-1] if errors else ("invalid", None)
            failures.append(kind)
            scanlog.emit(
                "agent",
                f"AI batch {i}/{len(batches)} failed ({_KIND_TEXT[kind]}) → keeping structured verdicts",
                level="warn",
                data={"kind": kind, "error": message},
            )
            if kind in _STOP_KINDS:
                stopped = kind
                break
            continue
        # Only a batch that came back with verdicts counts as researched: the
        # UI shows `ai_checked` as "AI researched".
        for e in batch:
            e.ai_checked = True
        by_name = {(v.ecosystem, v.name.lower()): v for v in result.verdicts}
        for e in batch:
            v = by_name.get((e.ecosystem, e.name.lower()))
            if v is None:
                continue
            _merge_ai(e, v)
            e.ai_result = v.model_dump(mode="json")  # kept for reuse by later scans
            scanlog.emit(
                "agent",
                f"AI: {e.ecosystem.value}:{e.name} → {e.verdict.value}"
                + (f" (successor {e.successor})" if e.successor else ""),
                level="result",
                data={"verdict": e.verdict.value, "successor": e.successor,
                      "confidence": e.confidence.value,
                      "citations": [s.url for s in e.sources]},
            )

    outcome.batches = len(batches)
    outcome.failed_batches = len(failures)
    outcome.skipped_batches = len(batches) - attempted
    outcome.seconds = round(time.monotonic() - t0, 1)
    outcome.tokens = totals
    outcome.status, outcome.detail = _outcome_status(
        len(batches), failures, outcome.skipped_batches, stopped,
    )
    scanlog.emit(
        "enrich",
        f"AI enrichment spent {totals.get('total_tokens', 0):,} tokens across "
        f"{len(batches)} batch(es) for {len(to_research)} package(s)",
        data=totals,
    )
    if outcome.status != "ok":
        scanlog.emit("enrich", f"AI research {outcome.status}: {outcome.detail}", level="warn")
    return candidates
