"""Agent 1 — Extraction.

Deterministic walk first (parsers.py), covering requirements.txt / package.json /
package-lock.json / pyproject.toml / Dockerfile. The AI agent is the *fallback*
for formats the parsers don't know — it runs scoped to the clone dir with the
built-in Read/Glob/Grep tools and returns extra packages as JSON, validated into
the same PackageList contract. This keeps the bulk reproducible and cheap.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from .. import scanlog
from ..config import get_settings
from ..models import Package, PackageList
from . import parsers
from .transport import run_text_agent, sum_tokens

_MANIFEST_NAMES = {
    "package.json", "package-lock.json", "pyproject.toml", "dockerfile",
}


def _is_manifest(path: Path) -> bool:
    n = path.name.lower()
    return (
        n in _MANIFEST_NAMES
        or n.startswith("dockerfile.")
        or (n.startswith("requirements") and n.endswith(".txt"))
        or parsers.is_compose_file(n)
    )


def walk_manifests(root: Path) -> list[Path]:
    found: list[Path] = []
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if parsers.under_skipped(p.relative_to(root).parts):
            continue
        if _is_manifest(p):
            found.append(p)
    return found


def dedup(packages: list[Package]) -> list[Package]:
    """Collapse duplicates by cache key; prefer a direct occurrence and one
    that carries a version."""
    best: dict[str, Package] = {}
    for pkg in packages:
        cur = best.get(pkg.key)
        if cur is None:
            best[pkg.key] = pkg
            continue
        if (pkg.version and not cur.version) or (pkg.is_direct and not cur.is_direct):
            best[pkg.key] = pkg
    return list(best.values())


def extract_vendored(clone_path: Path) -> list[Package]:
    """Vendored dependencies committed under `lib/` folders (common in plugin-style
    repos that ship their dependencies instead of a requirements.txt). `*.dist-info` gives an
    authoritative name+version; other top-level package folders give the name
    with no version."""
    packages: list[Package] = []
    for lib in clone_path.rglob("lib"):
        if not lib.is_dir():
            continue
        if parsers.under_skipped(lib.relative_to(clone_path).parts):
            continue
        rel = lib.relative_to(clone_path).as_posix()
        covered: set[str] = set()
        # 1. dist-info (authoritative)
        for di in lib.glob("*.dist-info"):
            pkg = parsers.parse_dist_info(di, rel)
            if pkg:
                packages.append(pkg)
                covered.add(pkg.name.lower().replace("-", "_"))
        # 2. source-only package folders not already covered by a dist-info
        for child in lib.iterdir():
            if child.name.lower().replace("-", "_") in covered:
                continue
            pkg = parsers.parse_source_package_dir(child, rel)
            if pkg:
                packages.append(pkg)
    return packages


def extract_deterministic(
    clone_path: Path, tag_vars: dict[str, str] | None = None
) -> list[Package]:
    packages: list[Package] = []
    for path in walk_manifests(clone_path):
        rel = path.relative_to(clone_path).as_posix()
        packages.extend(parsers.parse_file(path, rel, tag_vars))
    packages.extend(extract_vendored(clone_path))
    return dedup(packages)


_AGENT_SYSTEM = (
    "You are a software dependency extractor. You are given a cloned repository. "
    "Find dependency manifests the caller may have missed (go.mod, Gemfile, "
    "pom.xml, build.gradle, Cargo.toml, composer.json, *.csproj, conda envs, etc.) "
    "and extract their direct dependencies with pinned versions where available."
)

_AGENT_PROMPT_TMPL = """\
Explore this repository (cwd is the repo root) using Read/Glob/Grep and extract
open-source dependencies from any manifest formats NOT in this already-parsed list:
{known}

Return ONLY a JSON object, no prose, matching exactly:
{{"packages": [{{"name": "...", "version": "... or null", "ecosystem": "pypi|npm|apt|docker-image|other", "source_file": "relative/path", "is_direct": true}}]}}

If you find nothing new, return {{"packages": []}}.
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in agent output")
    return json.loads(text[start : end + 1])


async def extract_with_agent(
    clone_path: Path, known_files: list[str], *, retries: int = 1
) -> list[Package]:
    """Run the AI extraction agent for unknown manifest formats. Validated into
    PackageList; on repeated malformed output, returns [] (never fails the scan)."""
    prompt = _AGENT_PROMPT_TMPL.format(known=", ".join(sorted(set(known_files))) or "(none)")
    scanlog.emit("extract", f"Running AI extraction agent ({get_settings().agent_model}) for unknown formats",
                 level="info")
    last_err = ""
    spent: dict = {}
    for _ in range(retries + 1):
        res = await run_text_agent(
            prompt,
            cwd=str(clone_path),
            allowed_tools=["Read", "Glob", "Grep"],
            append_system=_AGENT_SYSTEM,
            add_dirs=[str(clone_path)],
        )
        sum_tokens(spent, res.tokens())
        scanlog.emit(
            "extract",
            f"Extraction agent tokens: {spent.get('total_tokens', 0):,} "
            f"({spent.get('input_tokens', 0):,} in / {spent.get('output_tokens', 0):,} out)",
            data=spent,
        )
        if res.is_error:
            last_err = res.error_message
            continue
        try:
            return PackageList(**_extract_json(res.text)).packages
        except Exception as e:  # noqa: BLE001 - malformed output → retry
            last_err = str(e)
    return []  # degrade gracefully


async def extract(
    clone_path: Path,
    *,
    use_agent: bool = False,
    tag_vars: dict[str, str] | None = None,
) -> PackageList:
    """Full extraction: deterministic parsers, plus the AI agent for the rest.
    `tag_vars` supplies values for compose `${VAR}` image tags.

    The deterministic walk is pure blocking I/O (it reads every candidate file in
    the clone), so it runs in a worker thread — a scan shares its event loop with
    the API, and stalling it there freezes every request for the duration."""
    packages = await asyncio.to_thread(extract_deterministic, clone_path, tag_vars)
    if use_agent:
        known = sorted({p.source_file for p in packages})
        packages.extend(await extract_with_agent(clone_path, known))
        packages = dedup(packages)
    return PackageList(packages=packages)
