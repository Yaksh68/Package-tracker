"""OS-package inventory of a Docker image (`dpkg -l` parity with the manual CSVs).

Runs `docker run --rm --entrypoint "" <image> dpkg-query -W …` and parses the
result into apt Packages with exact installed versions. These are marked
`is_direct=False` (they're the image's inventory, not the repo's declarations),
with `source_file="image:<ref>"` so the UI/CSV shows where they came from.

The image(s) to inventory are **auto-detected from the repo** (`detect_images`):
runtime images declared in docker-compose files, plus real base images from
Dockerfile `FROM` lines. No manual input needed — except for compose files that
take their tags from variables (`image: index.docker.io/${CORE_TAG}`), where the
concrete ref isn't in the repo at all; those resolve from the per-repo
`image_tags` input (`Repo.image_tags`, surfaced by `detect_tag_vars`).
Services that build their image locally (`build:`) are never candidates — their
tag exists in no registry, so a pull could only fail.

Requires Docker on the host and the image to be pullable/present. Any failure
degrades the scan (packages just missing) — it never breaks it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
from pathlib import Path

from ..models import Ecosystem, Package
from . import parsers

# conservative image-reference check; also blocks anything starting with "-"
# so a ref can never be interpreted as a docker CLI flag.
_IMAGE_REF = re.compile(r"^[a-z0-9][a-zA-Z0-9._\-/:@]*$")

_DPKG_CMD = ["dpkg-query", "-W", "-f", "${Package},${Version},${Architecture}\n"]

# Debian/Ubuntu package-name grammar (lowercase, digits, `. + -`). Deliberately
# stricter than the Dockerfile parser's token check: names reach a shell in
# resolve_apt_versions(), so this allowlist is what guarantees no metacharacter
# can ever be injected — anything outside it is dropped before the command runs.
_APT_PKG_NAME = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
# `apt-get install --simulate` prints `Inst <name> (<version> <origin> [arch])`
# for each package that would be newly installed.
_INST_RE = re.compile(r"^Inst\s+(?P<name>\S+)\s+\((?P<ver>\S+)")

_FROM_RE = re.compile(r"^\s*FROM\s+(?P<img>\S+)(?:\s+AS\s+(?P<alias>\S+))?", re.IGNORECASE)


class ImageInspectError(RuntimeError):
    pass


def validate_image_ref(image: str) -> str:
    image = (image or "").strip()
    if not image or not _IMAGE_REF.match(image):
        raise ImageInspectError(f"invalid image reference: {image!r}")
    return image



def _images_from_dockerfile(text: str) -> list[str]:
    """Real base images from `FROM` lines — skipping build-stage aliases,
    `scratch`, and refs with `$ARG` interpolation."""
    out: list[str] = []
    aliases: set[str] = set()
    for line in text.splitlines():
        m = _FROM_RE.match(line)
        if not m:
            continue
        img = m["img"]
        if m["alias"]:
            aliases.add(m["alias"].lower())
        if img.lower() in aliases or img.lower() == "scratch" or "$" in img:
            continue
        out.append(img)
    return out


def _compose_files(clone_path: Path):
    """(path, text) for every compose file in the clone, skipping vendored dirs."""
    for p in clone_path.rglob("*"):
        if p.is_dir() or parsers.under_skipped(p.relative_to(clone_path).parts):
            continue
        if parsers.is_compose_file(p.name.lower()):
            try:
                yield p, p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue


def detect_tag_vars(clone_path: Path) -> list[str]:
    """Env-var names the repo's compose files interpolate into `image:` refs
    (e.g. `CORE_TAG`, `MONGO_TAG`). These are the image tags a user can supply
    per repo so those images become trackable. Deduped, order-preserving."""
    seen: set[str] = set()
    out: list[str] = []
    for _, text in _compose_files(clone_path):
        for name in parsers.compose_tag_vars(text):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def detect_images(clone_path: Path, tag_vars: dict[str, str] | None = None) -> list[str]:
    """Auto-detect the Docker images worth inventorying for a repo: runtime
    images from docker-compose files first (the deployed artifacts), then base
    images from Dockerfiles. Compose `${VAR}` tags are resolved from `tag_vars`
    (user-supplied image tags); refs still unresolved after that are skipped.
    Deduped, order-preserving, filtered to valid references."""
    compose: list[str] = []
    dockerfiles: list[str] = []
    for p in clone_path.rglob("*"):
        if p.is_dir():
            continue
        if parsers.under_skipped(p.relative_to(clone_path).parts):
            continue
        name = p.name.lower()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if parsers.is_compose_file(name):
            compose.extend(parsers.compose_image_refs(text, tag_vars))
        elif name == "dockerfile" or name.startswith("dockerfile."):
            dockerfiles.extend(_images_from_dockerfile(text))

    seen: set[str] = set()
    ordered: list[str] = []
    for ref in [*compose, *dockerfiles]:  # compose (deployed) first
        try:
            ref = validate_image_ref(ref)
        except ImageInspectError:
            continue
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def parse_os_release(text: str) -> tuple[str, str] | None:
    """`(id, version_id)` from an `/etc/os-release` body — e.g.
    `("ubuntu", "24.04")`. None if either field is missing."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep:
            fields[name] = value.strip().strip('"').strip("'")
    os_id, version = fields.get("ID", "").lower(), fields.get("VERSION_ID", "")
    if not os_id or not version:
        return None
    return os_id, version


async def read_os_release(image: str, *, timeout: float = 120.0) -> tuple[str, str] | None:
    """The distribution an image is built on, from its `/etc/os-release`.

    This is the support window that actually governs the image's OS packages:
    `python3 3.12.3-0ubuntu2.1` is Canonical's build, maintained on Ubuntu's
    schedule, not upstream CPython's. Returns None for an image with no
    os-release (distroless/scratch) — the caller then has no distro window and
    leaves those packages unknown rather than guessing.
    """
    image = validate_image_ref(image)
    cmd = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "",
           image, "cat", "/etc/os-release"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ImageInspectError("docker is not installed / not on PATH") from e
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return parse_os_release(stdout.decode(errors="replace"))


def parse_dpkg_output(text: str, image: str) -> list[Package]:
    out: list[Package] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2 or not parts[0]:
            continue
        name, version = parts[0], parts[1] or None
        if name in seen:
            continue
        seen.add(name)
        out.append(Package(
            name=name, version=version, ecosystem=Ecosystem.apt,
            source_file=f"image:{image}", is_direct=False,
        ))
    return out


def _docker_cmd(image: str) -> list[str]:
    return ["docker", "run", "--rm", "--network", "none", "--entrypoint", "", image, *_DPKG_CMD]


def inspect_command_str(image: str) -> str:
    """Human-readable docker invocation for the activity trace."""
    return " ".join(_docker_cmd(validate_image_ref(image)))


async def list_image_packages(image: str, *, timeout: float = 300.0) -> list[Package]:
    """Inventory the installed OS packages of `image`. Pulls the image if needed
    (hence the generous timeout)."""
    image = validate_image_ref(image)
    cmd = _docker_cmd(image)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ImageInspectError("docker is not installed / not on PATH") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ImageInspectError(f"image inspection timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        msg = stderr.decode(errors="replace").strip()[:300]
        raise ImageInspectError(f"docker/dpkg-query failed for {image}: {msg}")
    return parse_dpkg_output(stdout.decode(errors="replace"), image)


# --------------------------------------------------------------------------- #
# Resolve unpinned Dockerfile apt versions against the base image              #
# --------------------------------------------------------------------------- #
# A Dockerfile like `FROM ubuntu:24.04` + `apt-get install python3.12` declares
# the package but not its version. The raw base image doesn't contain it yet
# (it's installed in a RUN layer we never build), so dpkg inventory can't see it
# and it lands in the scan with version=None — leaving the CVE-relevant patch
# level unknown. We recover it by asking the base image's apt what version it
# *would* install: `apt-get install --simulate` reports the exact candidate
# (including -updates/-security), which is what a build produces.

def _resolve_cmd(image: str, names: list[str]) -> list[str]:
    # Needs network for `apt-get update` (unlike dpkg inventory, which is
    # hermetic). Runs as the image's default (root) user; --simulate makes no
    # changes to the throwaway container. Errors are NOT swallowed: if
    # `apt-get update` can't reach the archive we emit a marker on stderr so an
    # empty result is diagnosable (network) rather than mistaken for "no such
    # package". `Inst` lines land on stdout; apt's chatter stays on stderr.
    inner = (
        "apt-get update -qq 1>/dev/null || echo APT_UPDATE_FAILED >&2; "
        "apt-get install -y --no-install-recommends --simulate "
        + " ".join(names)
    )
    return ["docker", "run", "--rm", "--entrypoint", "", image, "sh", "-c", inner]


# apt aborts the WHOLE simulated install when one name is unknown to the
# image's archive (msodbcsql18 lives in Microsoft's repo, not Ubuntu's) — so the
# offenders are dropped and the rest retried.
_APT_MISSING = re.compile(
    r"E: (?:Unable to locate package (?P<a>\S+)|Package '(?P<b>[^']+)' has no installation candidate)"
)


async def _simulate_install(image: str, names: list[str], timeout: float) -> tuple[str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *_resolve_cmd(image, names),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ImageInspectError("docker is not installed / not on PATH") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ImageInspectError(f"apt version resolution timed out after {timeout:.0f}s")
    return stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def resolve_apt_versions(
    image: str, names: list[str], *, timeout: float = 180.0
) -> dict[str, str]:
    """Return {name: version} for `names` as the base `image`'s apt would install
    them. Names failing the strict package-name allowlist are dropped (never sent
    to the shell). Returns only the packages apt reported an `Inst` line for; a
    non-apt image / offline host simply yields {} (caller degrades). Names the
    image's archive doesn't have are dropped and the rest retried."""
    image = validate_image_ref(image)
    names = [n for n in names if _APT_PKG_NAME.match(n)]
    want = set(names)
    resolved: dict[str, str] = {}
    for _ in range(3):
        if not names:
            break
        out, err = await _simulate_install(image, names, timeout)
        if "APT_UPDATE_FAILED" in err:
            raise ImageInspectError(f"apt-get update could not reach the archive for {image}")
        for line in out.splitlines():
            m = _INST_RE.match(line.strip())
            if m and m["name"] in want and m["name"] not in resolved:
                resolved[m["name"]] = m["ver"]
        missing = {m["a"] or m["b"] for m in _APT_MISSING.finditer(err)}
        if resolved or not missing:
            break
        names = [n for n in names if n not in missing]
    return resolved


# Base images that ARE a distribution — their tag is the release whose support
# window governs every OS package installed from its archive.
_DISTRO_IMAGES = frozenset({
    "ubuntu", "debian", "alpine", "rockylinux", "almalinux", "fedora",
    "centos", "opensuse", "opensuse/leap", "amazonlinux", "oraclelinux",
})


@dataclass
class DockerfileContext:
    """What each Dockerfile builds on, read from the files themselves.

    Taken from the clone rather than from the extracted packages: packages are
    deduped by name+version, so three Dockerfiles that all say
    `FROM ubuntu:24.04` leave ONE base-image package behind — and the other two
    Dockerfiles' apt installs would lose their distro (and version resolution)."""
    bases: dict[str, str] = field(default_factory=dict)      # source_file -> "ubuntu:24.04"
    apt_repos: dict[str, list[tuple[str, str, tuple[str, ...]]]] = field(default_factory=dict)


def dockerfile_context(clone_path: Path) -> DockerfileContext:
    ctx = DockerfileContext()
    for p in clone_path.rglob("*"):
        n = p.name.lower()
        if p.is_dir() or not (n == "dockerfile" or n.startswith("dockerfile.")):
            continue
        rel_parts = p.relative_to(clone_path).parts
        if parsers.under_skipped(rel_parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = p.relative_to(clone_path).as_posix()  # same naming as extraction's source_file
        if base := parsers.dockerfile_base(text):
            ctx.bases[rel] = base
        if repos := parsers.parse_apt_sources(text):
            ctx.apt_repos[rel] = repos
    return ctx


def distro_by_dockerfile(
    packages: list[Package], bases: dict[str, str] | None = None,
) -> dict[str, tuple[str, str]]:
    """`source_file -> (distro, release)` for Dockerfiles built on a distro base
    image, so apt packages a Dockerfile installs get the same distribution
    support window as the ones an image inventory finds. `bases` (from
    `dockerfile_context`) is preferred; without it the bases are recovered from
    the packages, which loses Dockerfiles sharing one base image."""
    out: dict[str, tuple[str, str]] = {}
    for source_file, ref in (bases if bases is not None else _base_image_by_source(packages)).items():
        name, _, tag = ref.rpartition(":")
        if name.lower() in _DISTRO_IMAGES and tag:
            out[source_file] = (name.lower(), tag)
    return out


def _base_image_by_source(packages: list[Package]) -> dict[str, str]:
    """source_file -> base image ref (`name:tag`) for each Dockerfile that
    declares one. Later FROM lines win (the final stage a build ends on)."""
    out: dict[str, str] = {}
    for p in packages:
        if p.ecosystem is Ecosystem.docker_image and p.version:
            out[p.source_file] = f"{p.name}:{p.version}"
    return out


async def reconcile_apt_versions(
    packages: list[Package],
    *,
    bases: dict[str, str] | None = None,
    emit=None,
    timeout: float = 180.0,
) -> list[Package]:
    """Fill in versions for version-less apt packages that a Dockerfile installs,
    by resolving them against that Dockerfile's base image. Returns a new list;
    packages that can't be resolved are left untouched. Never raises for a single
    image failing — it just leaves those versions as-is (degrade, don't break)."""
    base_by_file = bases if bases is not None else _base_image_by_source(packages)
    names_by_ref: dict[str, set[str]] = {}
    for p in packages:
        if p.ecosystem is Ecosystem.apt and not p.version:
            ref = base_by_file.get(p.source_file)
            if ref:
                names_by_ref.setdefault(ref, set()).add(p.name)
    if not names_by_ref:
        return packages

    resolved: dict[tuple[str, str], str] = {}
    for ref, names in names_by_ref.items():
        if emit:
            emit(f"Resolving {len(names)} unpinned apt version(s) against {ref}",
                 level="tool",
                 data={"image": ref, "command": " ".join(_resolve_cmd(ref, sorted(names)))})
        try:
            versions = await resolve_apt_versions(ref, sorted(names), timeout=timeout)
        except ImageInspectError as e:
            if emit:
                emit(f"apt version resolution failed for {ref}: {e}", level="error", data={})
            continue
        for name, ver in versions.items():
            resolved[(ref, name)] = ver
        if emit:
            emit(f"Resolved {len(versions)}/{len(names)} apt version(s) from {ref}",
                 level="result",
                 data={"image": ref, "resolved": versions})
    if not resolved:
        return packages

    out: list[Package] = []
    for p in packages:
        if p.ecosystem is Ecosystem.apt and not p.version:
            ref = base_by_file.get(p.source_file)
            ver = resolved.get((ref, p.name)) if ref else None
            if ver:
                out.append(p.model_copy(update={"version": ver}))
                continue
        out.append(p)
    return out
