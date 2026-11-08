"""Deterministic source lookups (httpx) for EOL/EOS signals.

These do the fetching/parsing; the enrichment agent decides which to call and
synthesizes the verdict. They are also usable directly (no AI) for the many
packages that resolve cleanly from structured sources — endoflife.date products,
PyPI/npm registry metadata, GitHub archived/staleness and SECURITY.md supported
versions — which keeps AI usage (and subscription cost) for the genuinely
ambiguous cases only.
"""
from __future__ import annotations

import contextvars
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from packaging.version import InvalidVersion, Version

from .. import scan_context, scanlog
from ..config import get_settings

_EOL_BASE = "https://endoflife.date/api/v1"
_TIMEOUT = httpx.Timeout(20.0)

# curated aliases: our extracted name -> endoflife slug
EOL_ALIASES = {
    "mongo": "mongodb",
    "mongodb-org": "mongodb",
    "node": "nodejs",
    "rabbitmq-server": "rabbitmq",
    "postgres": "postgresql",
    "ubuntu": "ubuntu",
    "python3": "python",
    "python3.12": "python",
    # `ENV OTP_VERSION=27.3.4.13` in a typical rabbitmq image — OTP is the Erlang
    # release train, tracked by endoflife.date under "erlang".
    "otp": "erlang",
}

# Package families shipped together with one product and versioned with it:
# MongoDB's apt repo splits the server into mongodb-org-server, -mongos, -shell,
# -tools, … all at the server's version, so the server's cycle applies. (mongosh
# and mongodb-database-tools have their own versions and no published EOL.)
EOL_PREFIX_ALIASES = (
    ("mongodb-org-", "mongodb"),
    ("mongodb-enterprise", "mongodb"),
)

_products_index_cache: list[dict] | None = None


def _to_date(v: Any) -> date | None:
    if isinstance(v, str):
        m = re.match(r"\d{4}-\d{2}-\d{2}", v)
        if m:
            try:
                return datetime.strptime(m.group(0), "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


# Failed lookups during one scan, counted by "host:kind", e.g.
# {"api.github.com:rate_limited": 3}. The pipeline starts a tally per scan and
# stores it on the scan's `scan_runs` row; outside a scan nothing is counted.
# A 404 is a normal answer here (endoflife.date doesn't track most packages),
# so only rate limits, server errors, bad responses and network errors count.
_upstream_errors: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "upstream_errors", default=None
)


def start_error_tally() -> dict[str, int]:
    """Count this task's failed lookups into a new dict (one per scan)."""
    tally: dict[str, int] = {}
    _upstream_errors.set(tally)
    return tally


def _tally(url: str, kind: str) -> None:
    tally = _upstream_errors.get()
    if tally is not None:
        key = f"{urlsplit(url).hostname or '?'}:{kind}"
        tally[key] = tally.get(key, 0) + 1


async def _get_json(client: httpx.AsyncClient, url: str, **kw) -> Any | None:
    try:
        r = await client.get(url, **kw)
    except httpx.HTTPError:
        _tally(url, "network")
        return None
    if r.status_code == 200:
        try:
            return r.json()
        except ValueError:
            _tally(url, "bad_response")
            return None
    if r.status_code == 429 or (r.status_code == 403 and "api.github.com" in url):
        _tally(url, "rate_limited")
    elif r.status_code >= 500:
        _tally(url, "server_error")
    return {"__status__": r.status_code}


async def endoflife_products(client: httpx.AsyncClient) -> list[dict]:
    global _products_index_cache
    if _products_index_cache is None:
        data = await _get_json(client, f"{_EOL_BASE}/products")
        result = (data or {}).get("result") if isinstance(data, dict) else None
        # A failed fetch is NOT cached. Storing `[]` would make every later
        # `resolve_eol_slug` miss for the rest of the process — one transient
        # blip at the start of a scan would silently strip the EOL dates off
        # every package except the handful in `EOL_ALIASES`, and the scan would
        # still look clean.
        if not result:
            return []  # retry on the next lookup rather than poisoning the cache
        _products_index_cache = result
    return _products_index_cache


async def resolve_eol_slug(client: httpx.AsyncClient, name: str) -> str | None:
    key = name.lower()
    if key in EOL_ALIASES:
        return EOL_ALIASES[key]
    for prefix, slug in EOL_PREFIX_ALIASES:
        if key.startswith(prefix):
            return slug
    for prod in await endoflife_products(client):
        names = {str(prod.get("name", "")).lower(), str(prod.get("label", "")).lower()}
        names |= {str(a).lower() for a in (prod.get("aliases") or [])}
        if key in names:
            return prod.get("name")
    return None


def _overall_latest(releases: list[dict], *, stable_only: bool = False) -> str | None:
    """The newest release version across ALL cycles — the true "latest
    available", not the latest within the current version's cycle. endoflife
    orders cycles newest-first, but we pick by parsed version to be safe.

    With `stable_only`, pre-releases (rc/beta/alpha/dev) are skipped and the
    result is None when no stable release exists — there is no prerelease
    fallback, because that is exactly what the plain `latest` is for."""
    names = [n for r in releases if (n := (r.get("latest") or {}).get("name"))]
    if not names:
        return None
    best = None
    best_v = None
    for n in names:
        try:
            v = Version(str(n))
        except InvalidVersion:
            continue
        if stable_only and v.is_prerelease:
            continue
        if best_v is None or v > best_v:
            best, best_v = n, v
    if stable_only:
        return best  # None when every cycle is a prerelease / unparseable
    return best or names[0]  # fall back to first (newest cycle) if none parse


async def query_endoflife(client: httpx.AsyncClient, name: str, version: str | None) -> dict:
    slug = await resolve_eol_slug(client, name)
    if not slug:
        return {"matched": False}
    data = await _get_json(client, f"{_EOL_BASE}/products/{slug}")
    result = (data or {}).get("result") if isinstance(data, dict) else None
    if not result:
        return {"matched": False, "slug": slug}
    releases = result.get("releases") or []
    chosen = None
    if version:
        vnorm = str(version)
        for rel in releases:
            rname = str(rel.get("name", ""))
            if vnorm == rname or vnorm.startswith(rname + ".") or rname == vnorm.split(".")[0]:
                chosen = rel
                break
    matched_cycle = chosen is not None
    if chosen is None and releases:
        chosen = releases[0]  # latest cycle as a fallback signal
    if chosen is None:
        return {"matched": True, "slug": slug}
    # `eolFrom` is the end of **security** support — the date after which the
    # cycle receives nothing at all. `eoasFrom` (end of *active* support) is
    # deliberately not returned: it only marks the switch to security-only
    # maintenance, and reporting it as "support ended" flagged cycles that are
    # still patched for years. `latest` is the overall newest available,
    # across all cycles.
    return {
        "matched": True,
        "slug": slug,
        "cycle": chosen.get("name"),
        "matched_cycle": matched_cycle,
        "is_eol": chosen.get("isEol"),
        "eol_date": _to_date(chosen.get("eolFrom")),
        "latest": _overall_latest(releases),
        "latest_stable": _overall_latest(releases, stable_only=True),
        "latest_in_cycle": (chosen.get("latest") or {}).get("name"),
        "url": f"https://endoflife.date/{slug}",
    }


def _all_files_yanked(files: list) -> bool:
    """A release is yanked when it has files and every one of them is yanked.
    (A version with no files at all is a stub, handled separately.)"""
    return bool(files) and all(bool(f.get("yanked")) for f in files if isinstance(f, dict))


def latest_stable_pypi(releases: dict) -> str | None:
    """Highest non-prerelease, non-yanked version from PyPI's `releases` map
    ({version: [file, …]}). None when nothing survives filtering — the caller
    keeps PyPI's own `info.version` as the plain `latest_version`."""
    best = None
    best_v = None
    for name, files in (releases or {}).items():
        if not files or _all_files_yanked(files if isinstance(files, list) else []):
            continue
        try:
            v = Version(str(name))
        except InvalidVersion:
            continue
        if v.is_prerelease:
            continue
        if best_v is None or v > best_v:
            best, best_v = name, v
    return best


# npm follows semver, where a prerelease is anything with a `-` suffix after the
# version core (1.0.0-beta.1). That convention — not PEP 440 — is the whole test,
# so no semver dependency is needed; `packaging` still parses the plain cores.
def latest_stable_npm(versions: dict) -> str | None:
    """Highest published npm version with no semver prerelease suffix. None when
    every published version is a prerelease — the caller keeps `dist-tags.latest`
    as the plain `latest_version`."""
    best = None
    best_v = None
    for name in (versions or {}):
        s = str(name)
        if "-" in s:
            continue  # semver prerelease (1.0.0-beta.1, 2.0.0-rc.1, …)
        try:
            v = Version(s)
        except InvalidVersion:
            continue
        if v.is_prerelease:
            continue
        if best_v is None or v > best_v:
            best, best_v = s, v
    return best


async def query_pypi(client: httpx.AsyncClient, name: str) -> dict:
    data = await _get_json(client, f"https://pypi.org/pypi/{name}/json")
    if not isinstance(data, dict) or "info" not in data:
        return {"found": False}
    info = data["info"]
    latest = info.get("version")
    releases = data.get("releases", {})
    last_release = None
    files = releases.get(latest) or []
    if files:
        last_release = _to_date(files[0].get("upload_time_iso_8601") or files[0].get("upload_time"))
    classifiers = info.get("classifiers") or []
    inactive = any("Inactive" in c for c in classifiers)
    urls = dict(info.get("project_urls") or {})
    if info.get("home_page"):
        urls.setdefault("Homepage", info["home_page"])
    repo = pick_github_repo(urls)
    return {
        "found": True,
        "latest_version": latest,
        "latest_stable_version": latest_stable_pypi(releases),
        "last_release": last_release,
        "yanked": bool(info.get("yanked")),
        "inactive": inactive,
        "repo_url": repo,
        "url": f"https://pypi.org/project/{name}/",
    }


async def query_npm(client: httpx.AsyncClient, name: str) -> dict:
    data = await _get_json(client, f"https://registry.npmjs.org/{name}")
    if not isinstance(data, dict) or "dist-tags" not in data:
        return {"found": False}
    latest = (data.get("dist-tags") or {}).get("latest")
    times = data.get("time") or {}
    last_release = _to_date(times.get(latest)) if latest else None
    ver_meta = (data.get("versions") or {}).get(latest, {}) if latest else {}
    deprecated = ver_meta.get("deprecated") or data.get("deprecated")
    return {
        "found": True,
        "latest_version": latest,
        "latest_stable_version": latest_stable_npm(data.get("versions") or {}),
        "last_release": last_release,
        "deprecated": deprecated if isinstance(deprecated, str) else bool(deprecated),
        "repo_url": _npm_github_repo(ver_meta.get("repository") or data.get("repository")),
        "url": f"https://www.npmjs.com/package/{name}",
    }


def _npm_github_repo(repository: Any) -> str | None:
    """npm `repository` is `{type, url}` or a shorthand string
    (`github:owner/repo`, bare `owner/repo`, or a full URL)."""
    raw = repository.get("url") if isinstance(repository, dict) else repository
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith("github:"):
        raw = "github.com/" + raw[len("github:"):]
    elif re.fullmatch(r"[\w.-]+/[\w.-]+", raw):
        raw = "github.com/" + raw
    slug = github_slug(raw)
    return f"https://github.com/{slug}" if slug else None


# Not anchored at the end: registries often link a sub-page (`/issues`,
# `/blob/main/CHANGES.md`, `/tree/master/docs`) rather than the repo root. The old
# end-anchored pattern turned those into a 404 slug, so e.g. gunicorn — whose
# first GitHub link on PyPI is its issue tracker — never had GitHub consulted.
_GH_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
# github.com/<these>/… are site pages, not repositories.
_GH_NON_OWNERS = {"sponsors", "orgs", "users", "features", "topics", "marketplace", "apps"}


def github_slug(url: str | None) -> str | None:
    """`owner/repo` from any github.com URL (sub-pages and `.git` included)."""
    m = _GH_RE.search(url or "")
    if not m or m.group(1).lower() in _GH_NON_OWNERS:
        return None
    repo = m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"{m.group(1)}/{repo}" if repo else None


# project_urls labels that name the code repository, most specific first.
_REPO_LABELS = ("source", "repository", "code", "github")


def pick_github_repo(project_urls: dict) -> str | None:
    """Canonical `https://github.com/owner/repo` from PyPI `project_urls`,
    preferring a link labelled as the source repo over e.g. "Issue tracker"."""
    def rank(label: str) -> int:
        label = label.lower()
        return next((i for i, k in enumerate(_REPO_LABELS) if k in label), len(_REPO_LABELS))

    for label, url in sorted(project_urls.items(), key=lambda kv: rank(str(kv[0]))):
        slug = github_slug(str(url))
        if slug:
            return f"https://github.com/{slug}"
    return None


# An archived repo whose description points somewhere else ("This library has
# moved to https://github.com/googleapis/google-cloud-python/tree/main/packages/
# google-crc32c") was relocated, not abandoned.
_MOVED_RE = re.compile(
    r"\b(?:moved|migrated|relocated)\b.*?(https?://github\.com/[^\s)\]>\"']+)",
    re.IGNORECASE | re.DOTALL,
)


def moved_to(description: str | None) -> str | None:
    """The github.com URL an archived repo's description says it moved to."""
    m = _MOVED_RE.search(description or "")
    return m.group(1).rstrip(".,;") if m else None


async def query_github_repo(client: httpx.AsyncClient, owner_repo: str) -> dict:
    slug = github_slug(owner_repo) or owner_repo.strip("/")
    headers = {"Accept": "application/vnd.github+json"}
    # the scan's workspace token, else the repo creator's sign-in token, else
    # (local mode) the env: see scan_context
    token = scan_context.github_api_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{slug}"
    data = await _get_json(client, url, headers=headers)
    if token and isinstance(data, dict) and data.get("__status__") == 401:
        # GitHub refused the token (revoked or expired): count it, say so once,
        # and carry on without it (a lower rate limit beats no data)
        _tally(url, "auth_rejected")
        if scan_context.reject_github_api_token():
            scanlog.emit("source", "GitHub rejected the token for API lookups (revoked or expired?); "
                         "continuing without one, at GitHub's lower anonymous rate limit. "
                         "Signing in again, or a new GitHub token in Settings, fixes this.", level="warn")
        headers.pop("Authorization")
        data = await _get_json(client, url, headers=headers)
    if not isinstance(data, dict) or data.get("__status__") or "archived" not in data:
        return {"found": False, "status": (data or {}).get("__status__")}
    return {
        "found": True,
        "archived": bool(data.get("archived")),
        "pushed_at": _to_date(data.get("pushed_at")),
        "url": data.get("html_url"),
        "slug": data.get("full_name") or slug,
        "default_branch": data.get("default_branch") or "HEAD",
        "moved_to": moved_to(data.get("description")) if data.get("archived") else None,
    }


# --------------------------------------------------------------------------- #
# GitHub security policy (SECURITY.md "Supported Versions" table)              #
# --------------------------------------------------------------------------- #
# This is what github.com/<owner>/<repo>/security renders. A maintainer marking a
# release line ❌ there is saying it is no longer supported — the same
# milestone as EOL/EOS here, just published as a table instead of a date.
_SECURITY_PATHS = ("SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md")
_UNSUPPORTED_RE = re.compile(
    r"❌|✗|✖|⛔|🚫|:x:|:no_entry|:heavy_multiplication_x:|\bno\b|unsupported|"
    r"not supported|end[- ]of[- ]life|\beol\b|deprecated",
    re.IGNORECASE,
)
_SUPPORTED_RE = re.compile(
    r"✅|✔|☑|:white_check_mark:|:heavy_check_mark:|\byes\b|supported|\blts\b",
    re.IGNORECASE,
)
_SPEC_RE = re.compile(r"^(<=|>=|<|>|=)?\s*v?(\d+(?:\.\d+)*)(?:\.[x*])?\s*([x*+])?$", re.IGNORECASE)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_security_policy(md: str) -> list[tuple[str, bool]]:
    """`(version spec, supported)` rows from every markdown table whose header
    mentions "version". Rows with no recognisable status are skipped."""
    rows: list[tuple[str, bool]] = []
    in_table = False
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            in_table = False
            continue
        cells = _cells(line)
        if not in_table:
            in_table = any("version" in c.lower() for c in cells)
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # header separator
        spec, status = cells[0], " ".join(cells[1:])
        # unsupported first: "not supported" also contains "supported"
        if _UNSUPPORTED_RE.search(status):
            rows.append((spec, False))
        elif _SUPPORTED_RE.search(status):
            rows.append((spec, True))
    return rows


def security_support_for(rows: list[tuple[str, bool]], version: str | None) -> tuple[bool, str] | None:
    """Whether `version` is a supported line per the policy rows, with the row
    that decided it. None when the table doesn't cover the version.

    Explicit ranges (`< 22.0`, `>= 3`, `5.1.x`, `4.*`) win. Otherwise a plain row
    names a release line: gunicorn lists `26.0.0 ✅`, which covers 26.2.0, so the
    row sharing the longest leading version prefix decides. A version newer than
    every listed row takes the newest row's status."""
    if not version or not rows:
        return None
    try:
        v = Version(version)
    except InvalidVersion:
        return None
    rel = v.release

    plain: list[tuple[tuple[int, ...], bool, str]] = []
    for spec, ok in rows:
        m = _SPEC_RE.match(spec.replace(" ", "") if spec[:1] in "<>=" else spec.strip())
        if not m:
            continue
        op, num, wild = m.group(1), m.group(2), m.group(3)
        parts = tuple(int(p) for p in num.split("."))
        wildcard = bool(wild) or bool(re.search(r"\.[x*]$", spec.strip(), re.IGNORECASE))
        if op:
            sv = Version(num)
            hit = {"<": v < sv, "<=": v <= sv, ">": v > sv, ">=": v >= sv, "=": v == sv}[op]
            if hit:
                return ok, spec
        elif wildcard:
            if wild == "+":
                if v >= Version(num):
                    return ok, spec
            elif rel[: len(parts)] == parts:
                return ok, spec
        else:
            plain.append((parts, ok, spec))

    def common_prefix(parts: tuple[int, ...]) -> int:
        n = 0
        for a, b in zip(parts, rel):
            if a != b:
                break
            n += 1
        return n

    def as_version(parts: tuple[int, ...]) -> Version:
        return Version(".".join(map(str, parts)))

    longest = max((common_prefix(p) for p, _, _ in plain), default=0)
    if longest:
        # Ties (`0.9 ✅` / `0.8 ❌` both share "0" with 0.8.3) go to the newest row
        # at or below the version, else the oldest row above it.
        cands = [r for r in plain if common_prefix(r[0]) == longest]
        below = [r for r in cands if as_version(r[0]) <= v]
        pick = (max(below, key=lambda r: as_version(r[0])) if below
                else min(cands, key=lambda r: as_version(r[0])))
        return pick[1], pick[2]
    if plain:
        newest = max(plain, key=lambda r: as_version(r[0]))
        if v > as_version(newest[0]):
            return newest[1], newest[2]
    return None


async def query_github_security_policy(
    client: httpx.AsyncClient, slug: str, version: str | None, *, ref: str = "HEAD"
) -> dict:
    """Read the repo's SECURITY.md (via raw.githubusercontent.com — no API rate
    limit) and decide whether `version` is still a supported release line."""
    for path in _SECURITY_PATHS:
        try:
            r = await client.get(f"https://raw.githubusercontent.com/{slug}/{ref}/{path}")
        except httpx.HTTPError:
            return {"found": False}
        if r.status_code != 200:
            continue
        rows = parse_security_policy(r.text)
        decided = security_support_for(rows, version)
        return {
            "found": True,
            "has_table": bool(rows),
            "supported": decided[0] if decided else None,
            "row": decided[1] if decided else None,
            "url": f"https://github.com/{slug}/security/policy",
        }
    return {"found": False}


# --------------------------------------------------------------------------- #
# Vendor lifecycle pages (products endoflife.date doesn't track)               #
# --------------------------------------------------------------------------- #
# Microsoft ODBC Driver for SQL Server (apt: msodbcsql17 / msodbcsql18). The
# table there lists "Mainstream support end" per driver major; extended support
# exists only under a paid SQL Server agreement, like Ubuntu's ESM, so the
# mainstream date is the EOL used here.
MSODBC_LIFECYCLE_URL = "https://learn.microsoft.com/en-us/sql/connect/odbc/support-lifecycle"
_MSODBC_PKG = re.compile(r"^msodbcsql(\d+)$", re.IGNORECASE)
_MSODBC_ROW = re.compile(
    r"<td>\s*ODBC Driver (?P<major>\d+) for SQL Server.*?</td>\s*"
    r"<td>\s*(?P<main>[A-Z][a-z]+ \d{1,2}, \d{4})",
    re.DOTALL,
)
_msodbc_cache: dict[str, date] | None = None


def parse_msodbc_lifecycle(html: str) -> dict[str, date]:
    """{driver major: mainstream support end} from Microsoft's lifecycle table."""
    out: dict[str, date] = {}
    for m in _MSODBC_ROW.finditer(html):
        try:
            out[m["major"]] = datetime.strptime(m["main"], "%B %d, %Y").date()
        except ValueError:
            continue
    return out


async def query_vendor_lifecycle(client: httpx.AsyncClient, name: str) -> dict:
    """EOL for packages whose vendor publishes a lifecycle page instead of being
    on endoflife.date. `matched` is False when `name` isn't one of them or the
    page couldn't be read (no claim either way)."""
    global _msodbc_cache
    m = _MSODBC_PKG.match(name)
    if not m:
        return {"matched": False}
    if _msodbc_cache is None:
        try:
            r = await client.get(MSODBC_LIFECYCLE_URL, follow_redirects=True)
            table = parse_msodbc_lifecycle(r.text) if r.status_code == 200 else {}
        except httpx.HTTPError:
            table = {}
        if not table:
            return {"matched": False}  # not cached: retry on the next lookup
        _msodbc_cache = table
    eol = _msodbc_cache.get(m.group(1))
    if eol is None:
        return {"matched": False}
    return {
        "matched": True,
        "product": f"ODBC Driver {m.group(1)} for SQL Server",
        "eol_date": eol,
        "url": MSODBC_LIFECYCLE_URL,
    }


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": "packages-tracker/0.1"},
        follow_redirects=True,
    )
