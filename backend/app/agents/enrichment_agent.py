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

from datetime import date

import httpx

import json
import re

from .. import scanlog, settings_store
from ..config import get_settings
from ..models import (
    AiVerdictBatch,
    Confidence,
    Ecosystem,
    Enrichment,
    Package,
    Source,
    Verdict,
)
from . import apt_index, source_apis
from .transport import run_text_agent, sum_tokens


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
) -> list[Enrichment]:
    """Enrich a list of packages against today's upstream state (no caching).

    Deduped by `ecosystem:name:version`, so the same pinned library appearing in
    several manifests costs one lookup per scan.

    `distro_by_source` maps a `source_file` to the distribution behind it
    (`{"image:ubuntu:24.04": ("ubuntu", "24.04")}`) — the support window applied
    to that source's distro-maintained OS packages."""
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
    # every scan when the toggle is on.
    if settings_store.get_runtime_settings().get("use_enrichment_agent"):
        await _ai_pass(list(results.values()), today=today)

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


async def _run_ai_batch(
    batch: list[Enrichment], *, max_searches: int
) -> tuple[AiVerdictBatch | None, dict]:
    """Return the parsed verdicts (or None) and the tokens this batch spent.

    A retry after malformed output is charged into the same total: the tokens
    were spent whether or not the second attempt parsed."""
    prompt = _AI_PROMPT_TMPL.format(
        max_searches=max_searches,
        packages=json.dumps([_signal_line(e) for e in batch], indent=2),
    )
    spent: dict = {}
    for _ in range(2):  # initial + one retry on malformed output
        res = await run_text_agent(
            prompt,
            allowed_tools=["WebSearch", "WebFetch"],
            append_system=_AI_SYSTEM,
            timeout=420.0,
        )
        sum_tokens(spent, res.tokens())
        if res.is_error:
            scanlog.emit("agent", f"AI enrichment batch error: {res.error_message}", level="warn")
            continue
        try:
            return AiVerdictBatch(**_extract_json(res.text)), spent
        except Exception as ex:  # noqa: BLE001 - malformed → retry then degrade
            scanlog.emit("agent", f"AI batch output invalid ({ex}); retrying", level="warn")
    return None, spent


async def _ai_pass(pool: list[Enrichment], *, today) -> list[Enrichment]:
    """Run the AI research agent over the ambiguous, not-yet-AI-checked entries in
    `pool`. Returns the candidates it processed (marked `ai_checked`)."""
    settings = get_settings()
    candidates = [
        e for e in pool
        if _needs_ai(e, stale_years=settings.stale_years) and not e.ai_checked
    ]
    if not candidates:
        scanlog.emit("enrich", "AI enrichment: 0 new ambiguous packages, skipped")
        return []

    batches = _chunk(candidates, settings.enrichment_batch_size)
    scanlog.emit(
        "enrich",
        f"AI enrichment: {len(candidates)} ambiguous packages → {len(batches)} batch(es)",
        data={"candidates": [f"{e.ecosystem.value}:{e.name}" for e in candidates]},
    )

    totals: dict = {}
    for batch in batches:
        result, spent = await _run_ai_batch(batch, max_searches=settings.web_search_max_uses)
        sum_tokens(totals, spent)
        scanlog.emit(
            "agent",
            f"Batch tokens: {spent.get('total_tokens', 0):,} "
            f"({spent.get('input_tokens', 0):,} in / {spent.get('output_tokens', 0):,} out) "
            f"over {len(batch)} package(s)",
            data=spent,
        )
        # Mark every attempted candidate as checked so we don't re-run (or re-spend)
        # on it next scan — even if the batch degraded or returned no verdict for it.
        for e in batch:
            e.ai_checked = True
        if result is None:
            scanlog.emit("agent", "AI batch degraded → keeping structured verdicts", level="warn")
            continue
        by_name = {(v.ecosystem, v.name.lower()): v for v in result.verdicts}
        for e in batch:
            v = by_name.get((e.ecosystem, e.name.lower()))
            if v is None:
                continue
            _merge_ai(e, v)
            scanlog.emit(
                "agent",
                f"AI: {e.ecosystem.value}:{e.name} → {e.verdict.value}"
                + (f" (successor {e.successor})" if e.successor else ""),
                level="result",
                data={"verdict": e.verdict.value, "successor": e.successor,
                      "confidence": e.confidence.value,
                      "citations": [s.url for s in e.sources]},
            )

    scanlog.emit(
        "enrich",
        f"AI enrichment spent {totals.get('total_tokens', 0):,} tokens across "
        f"{len(batches)} batch(es) for {len(candidates)} package(s)",
        data=totals,
    )
    return candidates
