"""Deterministic manifest parsers — run before the extraction agent.

They handle the common, well-specified formats (requirements.txt, package.json,
package-lock.json, Dockerfile, pyproject.toml) reliably and for free. The AI
extraction agent only has to cover what these miss (unknown/odd formats). Keeping
these deterministic means the bulk of extraction is reproducible and testable
offline, and the agent's turns stay short (cost control).
"""
from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path

import yaml

from ..models import Ecosystem, Package

# Directories no repo walk ever descends into: vendored/build output, VCS
# metadata, and agent tooling (`.claude/`) — the latter ships dev-only
# Dockerfiles/compose files that are not part of the product's dependency
# surface. Single source of truth: extraction_agent and image_inspect both
# import this, so a dir added here is skipped by every walk at once.
SKIP_DIRS = frozenset({
    ".git", ".claude", "node_modules", ".venv", "venv", "__pycache__", "dist",
    "build", ".mypy_cache", ".pytest_cache", "vendor", ".idea",
})


def under_skipped(rel_parts) -> bool:
    """True if any path component of a clone-relative path is a skipped dir."""
    return any(part in SKIP_DIRS for part in rel_parts)


# --------------------------------------------------------------------------- #
# requirements.txt                                                             #
# --------------------------------------------------------------------------- #
_REQ_LINE = re.compile(
    r"""^\s*
        (?P<name>[A-Za-z0-9._-]+)
        (?:\[[^\]]+\])?              # extras
        \s*==\s*
        (?P<version>[^\s;#]+)
    """,
    re.VERBOSE,
)
# also capture unpinned direct deps (no version) so they still surface
_REQ_NAME_ONLY = re.compile(r"^\s*(?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]+\])?\s*(?:[#;].*)?$")


def parse_requirements(text: str, source_file: str) -> list[Package]:
    out: list[Package] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue  # skip comments and -r/-e/--options
        m = _REQ_LINE.match(line)
        if m:
            out.append(Package(
                name=m["name"], version=m["version"],
                ecosystem=Ecosystem.pypi, source_file=source_file, is_direct=True,
            ))
            continue
        m2 = _REQ_NAME_ONLY.match(line)
        if m2 and not any(op in line for op in ("<", ">", "~", "!")):
            out.append(Package(
                name=m2["name"], version=None,
                ecosystem=Ecosystem.pypi, source_file=source_file, is_direct=True,
            ))
    return out


# --------------------------------------------------------------------------- #
# package.json / package-lock.json                                             #
# --------------------------------------------------------------------------- #
def _clean_npm_version(spec: str) -> str | None:
    if not spec or spec.startswith(("workspace:", "file:", "link:", "git+", "http")):
        return None
    return spec.lstrip("^~>=< v").split(" ")[0] or None


def parse_package_json(text: str, source_file: str) -> list[Package]:
    data = json.loads(text)
    out: list[Package] = []
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(field) or {}).items():
            out.append(Package(
                name=name, version=_clean_npm_version(str(spec)),
                ecosystem=Ecosystem.npm, source_file=source_file, is_direct=True,
            ))
    return out


def parse_package_lock(text: str, source_file: str) -> list[Package]:
    """Transitive npm deps from a lockfile (v2/v3 `packages` map)."""
    data = json.loads(text)
    out: list[Package] = []
    packages = data.get("packages")
    if isinstance(packages, dict):  # lockfile v2/v3
        for path, meta in packages.items():
            if not path or not path.startswith("node_modules/"):
                continue
            name = path.split("node_modules/")[-1]
            ver = meta.get("version")
            if name and ver:
                out.append(Package(
                    name=name, version=ver, ecosystem=Ecosystem.npm,
                    source_file=source_file, is_direct=False,
                ))
    else:  # lockfile v1
        for name, meta in (data.get("dependencies") or {}).items():
            ver = meta.get("version") if isinstance(meta, dict) else None
            if ver:
                out.append(Package(
                    name=name, version=ver, ecosystem=Ecosystem.npm,
                    source_file=source_file, is_direct=False,
                ))
    return out


# --------------------------------------------------------------------------- #
# Dockerfile — base images + explicitly installed apt packages                 #
# --------------------------------------------------------------------------- #
_FROM = re.compile(r"^\s*FROM\s+(?P<img>\S+)(?:\s+AS\s+(?P<alias>\S+))?", re.IGNORECASE)
# `install` may be preceded by flags (`apt-get -y install`, `apt-get --yes install`)
_APT_INSTALL = re.compile(r"apt(?:-get)?\s+(?:-{1,2}[\w=.\-]+\s+)*install\b", re.IGNORECASE)
_ARG_ENV = re.compile(r"^\s*(?P<kind>ARG|ENV)\s+(?P<rest>\S.*)$", re.IGNORECASE)
# `$VAR` / `${VAR}` / `${VAR:-default}` as used inside Dockerfile instructions.
_SHELL_VAR = re.compile(
    r"""\$(?:
          \{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::?[-+][^}]*)?\}
        | (?P<bare>[A-Za-z_][A-Za-z0-9_]*)
        )""",
    re.VERBOSE,
)
# `<PRODUCT>_VERSION=3.5.7` pins a component the build fetches from source
# (e.g. openssl, erlang/OTP, rabbitmq built in a Dockerfile) — no manifest declares it, so
# this ENV/ARG is the only place its version exists.
_VERSION_VAR = re.compile(r"^(?P<prod>[A-Z][A-Z0-9_]*)_VERSION$")
# ...but only when the prefix names a third-party product. These describe the
# image being built, not something with an upstream release stream.
_SELF_VERSION_VARS = frozenset({
    "app", "base", "build", "chart", "image", "package", "product",
    "project", "release", "schema", "src", "source",
})


def _expand_vars(value: str, env: dict[str, str]) -> str:
    """Substitute `$VAR`/`${VAR}` from `env`; unknown vars expand to ''
    (matching shell), which leaves a token that fails the name check below."""
    return _SHELL_VAR.sub(lambda m: env.get(m["braced"] or m["bare"], ""), value)


def _dockerfile_vars(joined: str) -> dict[str, str]:
    """ARG/ENV values declared in a Dockerfile, resolved in declaration order so
    later values can reference earlier ones (`ENV MONGO_PACKAGE=${MONGO_PACKAGE}`
    picks up the `ARG MONGO_PACKAGE=mongodb-org` default). Both the `NAME=value`
    and the legacy `ENV NAME value` forms are handled. Stage scoping is ignored:
    a flat map is enough to resolve the values, and a name is rarely reused with
    a different meaning across stages."""
    env: dict[str, str] = {}
    for line in joined.splitlines():
        m = _ARG_ENV.match(line)
        if not m:
            continue
        rest = m["rest"].strip()
        try:
            tokens = shlex.split(rest)
        except ValueError:  # unbalanced quotes — not worth guessing at
            continue
        if not tokens:
            continue
        if "=" in tokens[0]:
            for tok in tokens:
                name, sep, value = tok.partition("=")
                if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                    env[name] = _expand_vars(value, env)
        elif len(tokens) > 1 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tokens[0]):
            env[tokens[0]] = _expand_vars(" ".join(tokens[1:]), env)
        # bare `ARG NAME` declares without a value — nothing to resolve
    return env


def _version_var_packages(env: dict[str, str], source_file: str) -> list[Package]:
    """Components pinned only by a `<PRODUCT>_VERSION` ARG/ENV. The value must
    look like a real dotted version, so an `ARG NODE_VERSION=20` style major-only
    pin or a non-version string never invents a component."""
    out: list[Package] = []
    seen: set[str] = set()
    for name, value in env.items():
        m = _VERSION_VAR.match(name)
        if not m or not re.fullmatch(r"\d+(?:\.\d+)+[\w.\-]*", value):
            continue
        prod = m["prod"].lower().replace("_", "-")
        if prod in _SELF_VERSION_VARS or prod in seen:
            continue
        seen.add(prod)
        out.append(Package(
            name=prod, version=value, ecosystem=Ecosystem.other,
            source_file=source_file, is_direct=True,
        ))
    return out


def _split_image(img: str) -> tuple[str, str | None]:
    ref = img.split("@")[0]                       # drop digest
    if "/" in ref and ("." in ref.split("/")[0] or ":" in ref.split("/")[0]):
        ref = ref.split("/", 1)[1]                # drop registry host
    if ref.startswith("library/"):               # Docker official-image namespace
        ref = ref[len("library/"):]
    if ":" in ref:
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, None


# `deb [options] <url> <suite> <component...>` as Dockerfiles write it into
# /etc/apt/sources.list.d (e.g. Microsoft's repo for msodbcsql18).
_DEB_LINE = re.compile(
    r"""\bdeb\s+(?:\[[^\]]*\]\s+)?(?P<url>https?://[^\s"'>]+)\s+(?P<suite>[^\s"'>;&|]+)"""
    r"""(?P<comps>(?:[ \t]+[A-Za-z0-9][\w.-]*)*)""",
)


def parse_apt_sources(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Literal apt repos a Dockerfile adds: `[(url, suite, components)]`.
    Lines built from build ARGs (`${MONGO_REPO}`) are skipped — their URL isn't
    knowable from the repo alone."""
    joined = re.sub(r"\\\s*\n", " ", text)
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for m in _DEB_LINE.finditer(joined):
        url, suite = m["url"].rstrip("/"), m["suite"]
        if "$" in url or "$" in suite:
            continue
        comps = tuple(m["comps"].split()) or ("main",)
        if (url, suite, comps) not in out:
            out.append((url, suite, comps))
    return out


def dockerfile_base(text: str) -> str | None:
    """`name:tag` of the image the final stage builds on (stage aliases and
    scratch skipped), or None."""
    aliases: set[str] = set()
    base = None
    for line in text.splitlines():
        m = _FROM.match(line)
        if not m:
            continue
        img = m["img"]
        if m["alias"]:
            aliases.add(m["alias"].lower())
        if img.lower() in aliases or img.lower() == "scratch" or "$" in img:
            continue
        name, tag = _split_image(img)
        base = f"{name}:{tag}" if tag else name
    return base


def parse_dockerfile(text: str, source_file: str) -> list[Package]:
    out: list[Package] = []
    stage_aliases: set[str] = set()
    # Strip comments BEFORE collapsing line continuations — a `#` comment inside a
    # `\`-continued RUN block (common in these Dockerfiles) would otherwise be
    # merged into the apt-get install args and its words parsed as packages.
    # `#` counts as a comment only at line start or after whitespace (so URLs with
    # a #fragment are left intact).
    decommented = "\n".join(
        re.sub(r"(^|\s)#.*$", r"\1", ln) for ln in text.splitlines()
    )
    joined = re.sub(r"\\\s*\n", " ", decommented)
    env = _dockerfile_vars(joined)
    for line in text.splitlines():
        m = _FROM.match(line)
        if m:
            img = m["img"]
            if m["alias"]:
                stage_aliases.add(m["alias"].lower())
            if img.lower() in stage_aliases or img.lower() == "scratch":
                continue  # intermediate build stage or scratch, not a real base image
            name, tag = _split_image(img)
            out.append(Package(
                name=name, version=tag, ecosystem=Ecosystem.docker_image,
                source_file=source_file, is_direct=True,
            ))

    seen_apt: set[str] = set()
    for stmt in re.split(r"[;&\n]|&&", joined):
        m = _APT_INSTALL.search(stmt)
        if not m:
            continue
        after = stmt[m.end():]
        for tok in after.split():
            # `apt-get install ${MONGO_PACKAGE}=$MONGO_VERSION` names a real
            # package+version, but only after the build's ARG/ENV values are
            # substituted in — unresolved, it fails the name check below and the
            # pin is lost entirely.
            if "$" in tok:
                tok = _expand_vars(tok, env)
            if tok.startswith("-") or tok in ("y", "yes"):
                continue  # option flags / stray confirmation tokens
            # apt allows version pinning: nginx=1.28.0-1~jammy
            pkg, _, ver = tok.partition("=")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", pkg) or pkg in seen_apt:
                continue
            seen_apt.add(pkg)
            out.append(Package(
                name=pkg, version=ver if ver and ver[0].isdigit() else None,
                ecosystem=Ecosystem.apt, source_file=source_file, is_direct=True,
            ))
    out.extend(_version_var_packages(env, source_file))
    return out


# --------------------------------------------------------------------------- #
# docker-compose — runtime images declared per service                         #
# --------------------------------------------------------------------------- #
# Compose-style interpolation: ${VAR}, ${VAR:-default}, ${VAR-default}, $VAR.
_COMPOSE_VAR = re.compile(
    r"""\$(?:
          \{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>[^}]*))?\}
        | (?P<bare>[A-Za-z_][A-Za-z0-9_]*)
        )""",
    re.VERBOSE,
)


def _compose_image_strings(text: str) -> list[str]:
    """Raw `image:` values (interpolation intact) from every *pullable* service.

    A service carrying a `build:` key names an image it builds locally — the tag
    is a local build label (`integration-analyzer:latest`), not a registry
    artifact. Such refs have no upstream release stream to track for EOL and
    can't be pulled for dpkg inventory (`docker run` fails with "pull access
    denied"), so they're excluded here rather than at each call site.
    """
    out: list[str] = []
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return out
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        services = doc.get("services")
        if not isinstance(services, dict):
            continue
        for svc in services.values():
            if isinstance(svc, dict) and not svc.get("build"):
                img = svc.get("image")
                if isinstance(img, str) and img.strip():
                    out.append(img.strip())
    return out


def resolve_image_ref(ref: str, tag_vars: dict[str, str] | None = None) -> str | None:
    """Substitute compose env-var interpolation in an image ref using
    `tag_vars` (e.g. `{"CORE_TAG": "acme/platform-dev:core-latest"}` turns
    `index.docker.io/${CORE_TAG}` into a concrete ref). A `${VAR:-default}` falls
    back to its default. Returns None if any variable is left without a value —
    an unresolved ref is not a trackable image."""
    tag_vars = tag_vars or {}
    unresolved = False

    def sub(m: re.Match) -> str:
        nonlocal unresolved
        name = m["braced"] or m["bare"]
        val = (tag_vars.get(name) or "").strip()
        if not val:
            val = (m["default"] or "").strip()
        if not val:
            unresolved = True
            return ""
        return val

    out = _COMPOSE_VAR.sub(sub, ref).strip()
    if unresolved or not out:
        return None
    return out


def compose_tag_vars(text: str) -> list[str]:
    """Env-var names a compose file interpolates into its `image:` refs (e.g.
    `CORE_TAG`, `UI_TAG`). These are exactly the values a user must supply for
    the images to become trackable. Deduped, order-preserving."""
    seen: set[str] = set()
    out: list[str] = []
    for img in _compose_image_strings(text):
        for m in _COMPOSE_VAR.finditer(img):
            name = m["braced"] or m["bare"]
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def compose_image_refs(text: str, tag_vars: dict[str, str] | None = None) -> list[str]:
    """Runtime `image:` refs from every service in a compose file, with
    `${TAG}`-style interpolation resolved from `tag_vars` (user-supplied image
    tags). Refs whose variables have no value are skipped — the concrete tag
    isn't knowable from the repo alone. Single source of truth, also used by
    image_inspect.detect_images for dpkg targeting."""
    refs: list[str] = []
    for img in _compose_image_strings(text):
        if "$" in img:
            resolved = resolve_image_ref(img, tag_vars)
            if resolved:
                refs.append(resolved)
        else:
            refs.append(img)
    return refs


# Marks the span a user-supplied tag variable occupies in an otherwise raw ref,
# so we can tell *which part* of the reference came from that value. NUL can
# never occur in a real image reference.
_VAR_MARK = "\x00"


def _name_comes_from_supplied_tag(ref: str, tag_vars: dict[str, str]) -> bool:
    """True when the image *name* — not just its tag — is supplied out-of-band
    via `tag_vars` (`image: index.docker.io/${CORE_TAG}`).

    Such a reference names no upstream product. The repo can't name it because
    it isn't one: it's a release artifact the project's own pipeline publishes
    (`acme/platform-dev:core-…`), so there is no third-party release stream
    to have an EOL date, and the tag is a build label rather than a version. A
    deployment compose repo can be entirely of this shape — every service would
    otherwise land as one component named `acme/platform-dev` carrying several
    unrelated "versions", each sent to the AI research pass to come back `unknown`.

    A variable supplying only the *tag* (`mongo:${MONGO_TAG}`) is different —
    the repo does name the product, so that image stays trackable.
    """
    def sub(m: re.Match) -> str:
        name = m["braced"] or m["bare"]
        if (tag_vars.get(name) or "").strip():
            return _VAR_MARK
        return (m["default"] or "").strip()

    name, _ = _split_image(_COMPOSE_VAR.sub(sub, ref))
    return _VAR_MARK in name.split("/")[-1]


def parse_compose(
    text: str, source_file: str, tag_vars: dict[str, str] | None = None
) -> list[Package]:
    """Compose service images as trackable `docker-image` packages, so a compose
    repo yields EOL-trackable components.

    Images whose *name* only exists because the user supplied it as a tag
    variable are first-party build artifacts, not components — they are skipped
    here. They remain image-inspection targets (`compose_image_refs`, which
    `image_inspect.detect_images` uses, still returns them): their OS-package
    inventory is real data, their reference is not."""
    tag_vars = tag_vars or {}
    out: list[Package] = []
    seen: set[str] = set()
    for raw in _compose_image_strings(text):
        if "$" in raw:
            if _name_comes_from_supplied_tag(raw, tag_vars):
                continue
            ref = resolve_image_ref(raw, tag_vars)
            if not ref:
                continue
        else:
            ref = raw
        name, tag = _split_image(ref)
        key = f"{name}:{tag}"
        if key in seen:
            continue
        seen.add(key)
        out.append(Package(
            name=name, version=tag, ecosystem=Ecosystem.docker_image,
            source_file=source_file, is_direct=True,
        ))
    return out


def is_compose_file(name: str) -> bool:
    name = name.lower()
    return bool(re.fullmatch(r"docker-compose.*\.ya?ml", name)) or name in (
        "compose.yml", "compose.yaml", "podman-compose.yml", "podman-compose.yaml",
    )


# --------------------------------------------------------------------------- #
# pyproject.toml (PEP 621 + poetry)                                            #
# --------------------------------------------------------------------------- #
_DEP_SPEC = re.compile(r"^\s*(?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]+\])?\s*(?P<ver>==\s*[^\s;]+)?")


def parse_pyproject(text: str, source_file: str) -> list[Package]:
    data = tomllib.loads(text)
    out: list[Package] = []
    for dep in (data.get("project", {}).get("dependencies") or []):
        m = _DEP_SPEC.match(dep)
        if m:
            ver = m["ver"].replace("==", "").strip() if m["ver"] else None
            out.append(Package(name=m["name"], version=ver, ecosystem=Ecosystem.pypi,
                               source_file=source_file, is_direct=True))
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies") or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        ver = None
        if isinstance(spec, str):
            ver = spec.lstrip("^~>=< ") or None
        out.append(Package(name=name, version=ver, ecosystem=Ecosystem.pypi,
                           source_file=source_file, is_direct=True))
    return out


# --------------------------------------------------------------------------- #
# vendored packages under lib/ (dist-info + source-only folders)              #
# --------------------------------------------------------------------------- #
_DISTINFO_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.\-]+?)-(?P<version>\d[A-Za-z0-9_.!+\-]*)\.dist-info$"
)
_INIT_VERSION_RE = re.compile(
    r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE
)
# junk that appears at the top level of a vendored lib/ dir but isn't a package
_LIB_NON_PACKAGE = {"bin", "__pycache__"}


def parse_dist_info(dist_info_dir: Path, source_file: str) -> Package | None:
    """Authoritative name+version from a `*.dist-info` dir (METADATA if present,
    else the directory name)."""
    name = ver = None
    meta = dist_info_dir / "METADATA"
    if meta.exists():
        try:
            for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name:") and not name:
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("Version:") and not ver:
                    ver = line.split(":", 1)[1].strip()
                if name and ver:
                    break
        except OSError:
            pass
    # a version must look like one (starts with a digit); else fall back / drop
    if ver and not re.match(r"^\d", ver):
        ver = None
    if not (name and ver):
        m = _DISTINFO_RE.match(dist_info_dir.name)
        if m:
            name = name or m["name"]
            ver = ver or m["version"]
    if not name:
        return None
    return Package(name=name, version=ver, ecosystem=Ecosystem.pypi,
                   source_file=source_file, is_direct=True)


def _read_init_version(pkg_dir: Path) -> str | None:
    for cand in ("__init__.py", "version.py", "_version.py"):
        f = pkg_dir / cand
        if f.exists():
            try:
                m = _INIT_VERSION_RE.search(f.read_text(encoding="utf-8", errors="replace"))
                if m:
                    return m.group(1)
            except OSError:
                pass
    return None


def parse_source_package_dir(pkg_dir: Path, source_file: str) -> Package | None:
    """A vendored, source-only package folder (no dist-info). Name = folder name,
    version = best-effort from __version__, else None."""
    if not pkg_dir.is_dir() or pkg_dir.name in _LIB_NON_PACKAGE or pkg_dir.name.startswith("."):
        return None
    if pkg_dir.name.endswith((".dist-info", ".egg-info", ".data")):
        return None
    # only treat it as a package if it looks like one
    if not (pkg_dir / "__init__.py").exists() and not any(pkg_dir.glob("*.py")):
        return None
    ver = _read_init_version(pkg_dir)
    # reject false positives (e.g. the "." from `".".join(version_info)`)
    if ver and not re.match(r"^\d", ver):
        ver = None
    return Package(name=pkg_dir.name, version=ver,
                   ecosystem=Ecosystem.pypi, source_file=source_file, is_direct=True)


# --------------------------------------------------------------------------- #
# dispatch                                                                     #
# --------------------------------------------------------------------------- #
def parse_file(path: Path, rel: str, tag_vars: dict[str, str] | None = None) -> list[Package]:
    """Route a single file to the right parser by name. Returns [] if unknown.
    `tag_vars` supplies values for `${VAR}` image tags in compose files."""
    name = path.name.lower()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if name == "package.json":
        return parse_package_json(text, rel)
    if name == "package-lock.json":
        return parse_package_lock(text, rel)
    if name == "pyproject.toml":
        return parse_pyproject(text, rel)
    if name.startswith("requirements") and name.endswith(".txt"):
        return parse_requirements(text, rel)
    if name == "dockerfile" or name.startswith("dockerfile."):
        return parse_dockerfile(text, rel)
    if is_compose_file(name):
        return parse_compose(text, rel, tag_vars)
    return []
