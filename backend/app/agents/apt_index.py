"""apt package indexes — what a Debian-family image would install, and whether
the distribution itself publishes a given package version.

Three questions the scan used to leave as UNKNOWN:

* A Dockerfile says `apt-get install curl` with no version: which version does
  its `FROM ubuntu:24.04` install today? → the candidate in the series' index.
* An image inventory lists `apt 2.8.3` (no `ubuntu` revision in the string):
  is that Ubuntu's own build, maintained for the life of the release? → it is
  if the series publishes exactly that version (index, else Launchpad, which
  also knows superseded versions).
* `libc-dev` / `libncurses5-dev` are not real packages — they are virtual names
  another package `Provides:`. → resolved through the index's Provides lines.

Indexes are the `Packages` files apt itself downloads (`dists/<suite>/<comp>/
binary-<arch>/Packages.xz`), for Ubuntu's archive plus any third-party repo a
Dockerfile adds with a literal `deb <url> <suite> <comp>` line (e.g. Microsoft's
packages.microsoft.com for msodbcsql18). They are cached on disk: a release
pocket (`noble`) never changes once released and is kept for good; `-updates`,
`-security` and third-party suites are refreshed after `_REFRESH`.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import lzma
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .. import scanlog
from ..config import get_settings

UBUNTU_ARCHIVE = "http://archive.ubuntu.com/ubuntu"
UBUNTU_COMPONENTS = ("main", "restricted", "universe", "multiverse")
_LAUNCHPAD = "https://launchpad.net/api/devel"  # api.launchpad.net is unreachable from some networks
_REFRESH = 6 * 3600  # seconds before a mutable suite's index is re-downloaded
_ARCH = "amd64"

# Fallback when Launchpad's series list can't be fetched.
_UBUNTU_CODENAMES = {
    "18.04": "bionic", "20.04": "focal", "22.04": "jammy", "24.04": "noble",
    "24.10": "oracular", "25.04": "plucky", "25.10": "questing", "26.04": "resolute",
}


# --------------------------------------------------------------------------- #
# Debian version ordering (dpkg's algorithm)                                   #
# --------------------------------------------------------------------------- #
def _order(c: str) -> int:
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    if c == "~":
        return -1
    return ord(c) + 256 if c else 0


def _cmp_part(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        diff = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not diff:
                diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if diff:
            return diff
    return 0


def _split(v: str) -> tuple[int, str, str]:
    epoch, _, rest = v.partition(":") if ":" in v else ("0", "", v)
    upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
    try:
        return int(epoch), upstream, revision
    except ValueError:
        return 0, upstream, revision


def compare_versions(a: str, b: str) -> int:
    """<0, 0, >0 like dpkg --compare-versions (`1:2.4 > 3.0`, `1.0~rc1 < 1.0`)."""
    ea, ua, ra = _split(a)
    eb, ub, rb = _split(b)
    if ea != eb:
        return ea - eb
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)


# --------------------------------------------------------------------------- #
# repos + index                                                                #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AptRepo:
    url: str                  # e.g. http://archive.ubuntu.com/ubuntu
    suite: str                # noble, noble-updates, prod, ...
    components: tuple[str, ...]
    label: str                # "ubuntu 24.04" or the repo host

    @property
    def mutable(self) -> bool:
        # an Ubuntu release pocket is frozen once released; nothing else is
        return not (self.label.startswith("ubuntu") and "-" not in self.suite)


@dataclass(frozen=True)
class Hit:
    """One package version as an index publishes it."""
    name: str                 # the real package (the provider for a virtual name)
    version: str
    suite: str
    component: str
    label: str                # which repo: "ubuntu 24.04" / "packages.microsoft.com"
    provided_by: str | None = None  # set when the asked-for name is virtual

    @property
    def is_ubuntu(self) -> bool:
        return self.label.startswith("ubuntu")


@dataclass
class AptIndex:
    packages: dict[str, list[Hit]] = field(default_factory=dict)
    provides: dict[str, list[Hit]] = field(default_factory=dict)
    series: str | None = None  # Ubuntu codename the index was built for

    def __len__(self) -> int:
        return len(self.packages)

    def candidate(self, name: str) -> Hit | None:
        """What `apt-get install name` would pick: the highest version of the
        package, else of a package providing it."""
        hits = self.packages.get(name)
        if hits:
            return max(hits, key=_VersionKey)
        prov = self.provides.get(name)
        if prov:
            best = max(prov, key=_VersionKey)
            return Hit(name=best.name, version=best.version, suite=best.suite,
                       component=best.component, label=best.label, provided_by=best.name)
        return None

    def published(self, name: str, version: str) -> Hit | None:
        """The index entry for exactly `name` at `version`, if one exists
        (directly, or a provider whose `Provides: name (= version)` matches)."""
        for h in self.packages.get(name, []):
            if h.version == version:
                return h
        for h in self.provides.get(name, []):
            if h.version == version:
                return Hit(name=h.name, version=h.version, suite=h.suite,
                           component=h.component, label=h.label, provided_by=h.name)
        return None

    def merge(self, other: "AptIndex") -> "AptIndex":
        out = AptIndex(series=self.series or other.series)
        for src in (self, other):
            for k, v in src.packages.items():
                out.packages.setdefault(k, []).extend(v)
            for k, v in src.provides.items():
                out.provides.setdefault(k, []).extend(v)
        return out


class _VersionKey:
    __slots__ = ("v",)

    def __init__(self, hit: Hit) -> None:
        self.v = hit.version

    def __lt__(self, other: "_VersionKey") -> bool:
        return compare_versions(self.v, other.v) < 0


_PROVIDE = re.compile(r"([A-Za-z0-9][A-Za-z0-9.+-]*)(?:\s*\(\s*=\s*([^)\s]+)\s*\))?")


def parse_packages(text: str, *, suite: str, component: str, label: str, into: AptIndex) -> None:
    """Fold one `Packages` file into `into` (only Package/Version/Provides are read)."""
    def flush(name, version, provides):
        if not (name and version):
            return
        hit = Hit(name=name, version=version, suite=suite, component=component, label=label)
        into.packages.setdefault(name, []).append(hit)
        for m in _PROVIDE.finditer(provides or ""):
            virt = m.group(1)
            # `Provides: foo` without a version provides it at the package's version
            into.provides.setdefault(virt, []).append(
                Hit(name=name, version=m.group(2) or version, suite=suite,
                    component=component, label=label))

    name = version = provides = None
    for line in text.splitlines():
        if not line:
            flush(name, version, provides)
            name = version = provides = None
        elif line.startswith("Package: "):
            name = line[9:].strip()
        elif line.startswith("Version: "):
            version = line[9:].strip()
        elif line.startswith("Provides: "):
            provides = line[10:]
    flush(name, version, provides)


# --------------------------------------------------------------------------- #
# fetching + caching                                                           #
# --------------------------------------------------------------------------- #
def _cache_dir() -> Path:
    d = Path(get_settings().apt_index_cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _fetch_packages(client: httpx.AsyncClient, repo: AptRepo, component: str) -> str | None:
    """Decompressed `Packages` text for one repo component, via the disk cache.
    None when the repo doesn't publish it (or the network failed)."""
    base = f"{repo.url.rstrip('/')}/dists/{repo.suite}/{component}/binary-{_ARCH}/Packages"
    stem = _cache_dir() / hashlib.sha1(base.encode()).hexdigest()[:16]
    # kept compressed as downloaded (~25 MB for a whole Ubuntu series, not ~120 MB)
    cached = next((stem.with_suffix(ext) for ext in _FORMATS if stem.with_suffix(ext).is_file()), None)
    if cached and (not repo.mutable or time.time() - cached.stat().st_mtime < _REFRESH):
        return await asyncio.to_thread(_decode, cached)
    for ext in _FORMATS:
        try:
            r = await client.get(base + ("" if ext == ".raw" else ext), timeout=120)
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        target = stem.with_suffix(ext)
        await asyncio.to_thread(target.write_bytes, r.content)
        try:
            text = await asyncio.to_thread(_decode, target)
        except (lzma.LZMAError, OSError, EOFError):
            target.unlink(missing_ok=True)
            continue
        if cached and cached != target:
            cached.unlink(missing_ok=True)
        return text
    # offline / mirror hiccup: a stale copy beats nothing
    if cached:
        return await asyncio.to_thread(_decode, cached)
    return None


_FORMATS = (".xz", ".gz", ".raw")


def _decode(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".xz":
        data = lzma.decompress(data)
    elif path.suffix == ".gz":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")


async def build_index(client: httpx.AsyncClient, repos: list[AptRepo]) -> AptIndex:
    idx = AptIndex()
    for repo in repos:
        for comp in repo.components:
            text = await _fetch_packages(client, repo, comp)
            if text is None:
                scanlog.emit("source", f"apt index unavailable: {repo.url} {repo.suite}/{comp}",
                             level="warn")
                continue
            await asyncio.to_thread(parse_packages, text, suite=repo.suite, component=comp,
                                    label=repo.label, into=idx)
    return idx


_codenames: dict[str, str] | None = None


async def ubuntu_codename(client: httpx.AsyncClient, version: str) -> str | None:
    """`24.04` → `noble`, from Launchpad's series list (static map as fallback)."""
    global _codenames
    if _codenames is None:
        try:
            r = await client.get(f"{_LAUNCHPAD}/ubuntu/series", timeout=30)
            entries = r.json().get("entries", []) if r.status_code == 200 else []
            found = {e["version"]: e["name"] for e in entries if e.get("version") and e.get("name")}
        except (httpx.HTTPError, ValueError, KeyError):
            found = {}
        if found:
            _codenames = found  # only a successful fetch is kept
        else:
            return _UBUNTU_CODENAMES.get(version)
    return _codenames.get(version) or _UBUNTU_CODENAMES.get(version)


def ubuntu_repos(codename: str, version: str) -> list[AptRepo]:
    label = f"ubuntu {version}"
    return [AptRepo(UBUNTU_ARCHIVE, s, UBUNTU_COMPONENTS, label)
            for s in (codename, f"{codename}-updates", f"{codename}-security")]


# Process-wide memo: one parsed index per (distro, extra repos), rebuilt after _REFRESH.
_memo: dict[tuple, tuple[float, AptIndex]] = {}


async def index_for(
    client: httpx.AsyncClient, distro: tuple[str, str] | None, extra: tuple[AptRepo, ...] = (),
) -> AptIndex | None:
    """The index apt would see in an image of `distro` with `extra` repos added.
    None when there is nothing to index (non-Ubuntu distro and no extra repos)."""
    key = (distro, extra)
    hit = _memo.get(key)
    if hit and time.monotonic() - hit[0] < _REFRESH:
        return hit[1]
    repos: list[AptRepo] = []
    series = None
    if distro and distro[0] == "ubuntu":
        series = await ubuntu_codename(client, distro[1])
        if series:
            repos += ubuntu_repos(series, distro[1])
    repos += list(extra)
    if not repos:
        return None
    idx = await build_index(client, repos)
    idx.series = series
    _memo[key] = (time.monotonic(), idx)
    return idx


async def launchpad_published(
    client: httpx.AsyncClient, codename: str, name: str, version: str, *, label: str,
) -> Hit | None:
    """Did Ubuntu `codename` ever publish binary `name` at exactly `version`?
    Covers versions the current index no longer lists (superseded by a later
    update), which an image built a while ago commonly carries."""
    try:
        r = await client.get(
            f"{_LAUNCHPAD}/ubuntu/+archive/primary",
            params={"ws.op": "getPublishedBinaries", "binary_name": name, "version": version,
                    "exact_match": "true",
                    "distro_arch_series": f"{_LAUNCHPAD}/ubuntu/{codename}/{_ARCH}"},
            timeout=30,
        )
        entries = r.json().get("entries", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    # a "Deleted" -proposed upload alone doesn't count; it never reached users
    real = [e for e in entries if e.get("pocket") != "Proposed"]
    if not real:
        return None
    e = real[0]
    pocket = str(e.get("pocket") or "Release").lower()
    return Hit(name=name, version=version,
               suite=codename if pocket == "release" else f"{codename}-{pocket}",
               component=e.get("component_name") or "main", label=label)


# --------------------------------------------------------------------------- #
# scan helpers                                                                 #
# --------------------------------------------------------------------------- #
async def indexes_for_sources(
    client: httpx.AsyncClient,
    sources: set[str],
    distro_by_source: dict[str, tuple[str, str]],
    repos_by_source: dict[str, list[tuple[str, str, tuple[str, ...]]]],
) -> dict[str, AptIndex]:
    """`source_file -> AptIndex` for every source with apt packages whose image
    is Ubuntu or whose Dockerfile adds literal apt repos. Sources sharing a
    distro + repo set share one index."""
    out: dict[str, AptIndex] = {}
    for src in sorted(sources):
        distro = distro_by_source.get(src)
        extra = tuple(
            AptRepo(url, suite, comps, label=httpx.URL(url).host or url)
            for url, suite, comps in repos_by_source.get(src, [])
        )
        idx = await index_for(client, distro, extra)
        if idx is not None and len(idx):
            out[src] = idx
    return out


def resolve_unpinned(packages: list, indexes: dict[str, AptIndex]) -> tuple[list, dict[str, str]]:
    """Give version-less apt packages the version their image's apt would
    install (a virtual name gets its provider's version). Returns the new list
    and `{name: version}` for what was filled in."""
    out, filled = [], {}
    for p in packages:
        idx = indexes.get(p.source_file)
        if p.ecosystem.value == "apt" and not p.version and idx is not None:
            hit = idx.candidate(p.name)
            if hit:
                out.append(p.model_copy(update={"version": hit.version}))
                filled[p.name] = hit.version + (f" (via {hit.provided_by})" if hit.provided_by else "")
                continue
        out.append(p)
    return out, filled
