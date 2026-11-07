"""Pydantic models — the data contracts shared by agents, storage, and the API."""
from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Extraction (Agent 1 output contract)                                        #
# --------------------------------------------------------------------------- #
class Ecosystem(str, Enum):
    pypi = "pypi"
    npm = "npm"
    apt = "apt"
    docker_image = "docker-image"
    other = "other"


class Package(BaseModel):
    name: str
    version: str | None = None
    ecosystem: Ecosystem
    source_file: str
    is_direct: bool = True

    @property
    def key(self) -> str:
        """Dedup / lookup key: ecosystem:name:version."""
        return f"{self.ecosystem.value}:{self.name.lower()}:{self.version or ''}"


class PackageList(BaseModel):
    packages: list[Package] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Enrichment (Agent 2 output contract)                                        #
# --------------------------------------------------------------------------- #
class Verdict(str, Enum):
    supported = "supported"
    deprecated = "deprecated"
    eol = "eol"
    unknown = "unknown"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class Source(BaseModel):
    url: str
    note: str = ""
    origin: str = "structured"  # "structured" (endoflife/PyPI/npm/GitHub) | "web" (AI web research)


class Enrichment(BaseModel):
    name: str
    version: str | None = None
    ecosystem: Ecosystem
    verdict: Verdict = Verdict.unknown
    eol_date: date | None = None
    # End of **security** support — the same milestone as `eol_date`, which is
    # why it mirrors it. The earlier end-of-*active*-support date (endoflife.date
    # `eoasFrom`) is deliberately not tracked: it marks the switch to
    # security-only maintenance, not the end of support. Snapshots written before
    # this change may still hold that older, earlier date.
    eos_date: date | None = None
    latest_version: str | None = None
    # Newest non-prerelease release. Distinct from `latest_version`, which is
    # whatever upstream calls "latest" and may be an rc/beta. None when no stable
    # release exists (or the entry predates this field — it defaults in, so old
    # snapshots load unchanged).
    latest_stable_version: str | None = None
    successor: str | None = None
    archived: bool | None = None
    # Upstream SECURITY.md "Supported Versions" verdict for this version's
    # release line (what github.com/<repo>/security shows) — read only as an EOL
    # signal. False = the maintainers list this line as unsupported, i.e. EOL
    # with no published date. None = no policy / table doesn't cover it.
    security_supported: bool | None = None
    security_policy_row: str | None = None  # the table row that decided it, e.g. "24.1.1"
    last_release: date | None = None
    confidence: Confidence = Confidence.low
    summary: str = ""
    sources: list[Source] = Field(default_factory=list)
    ai_checked: bool = False  # has the AI research pass already examined this?

    @property
    def key(self) -> str:
        return f"{self.ecosystem.value}:{self.name.lower()}:{self.version or ''}"


class EnrichmentList(BaseModel):
    items: list[Enrichment] = Field(default_factory=list)


class AiVerdict(BaseModel):
    """AI research agent output for one package. Deliberately has NO date fields —
    EOL/EOS dates come only from structured sources; the model never sets dates."""
    name: str
    ecosystem: Ecosystem
    verdict: Verdict = Verdict.unknown
    successor: str | None = None
    confidence: Confidence = Confidence.low
    summary: str = ""
    sources: list[Source] = Field(default_factory=list)


class AiVerdictBatch(BaseModel):
    verdicts: list[AiVerdict] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Flagging engine output                                                       #
# --------------------------------------------------------------------------- #
class FlagStatus(str, Enum):
    ALREADY_EOL = "ALREADY_EOL"
    VULNERABLE = "VULNERABLE"  # retired: legacy backups (backend/data) still load
    EOL_BEFORE_RELEASE = "EOL_BEFORE_RELEASE"
    EOL_SOON_AFTER_RELEASE = "EOL_SOON_AFTER_RELEASE"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"
    EOS_PASSED = "EOS_PASSED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    OK = "OK"
    # On the repo's ignore list (e.g. test-only tooling): still listed, never
    # counted as risk.
    IGNORED = "IGNORED"


class Flag(BaseModel):
    key: str
    status: FlagStatus
    reasons: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Repo registry                                                                #
# --------------------------------------------------------------------------- #
# Compose files can take their image tags from variables
# (`image: index.docker.io/${CORE_TAG}`), so the concrete ref isn't in the repo
# at all. `Repo.image_tags` is the user-supplied {VAR: image-ref} map that makes
# those images trackable. Values are interpolated into refs that end up on a
# `docker run` command line, so both halves are validated here at the edge.
_TAG_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TAG_VAR_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")


def clean_image_tags(value) -> dict[str, str]:
    """Normalize + validate an image-tag map. Entries with an empty value are
    dropped (a blank input means "no value supplied"), so an entry that survives
    is always usable. Raises ValueError on a malformed name or reference."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("image_tags must be an object of VAR -> image reference")
    out: dict[str, str] = {}
    for raw_name, raw_val in value.items():
        name = str(raw_name).strip()
        val = "" if raw_val is None else str(raw_val).strip()
        if not val:
            continue
        if not _TAG_VAR_NAME.match(name):
            raise ValueError(f"invalid image-tag variable name: {name!r}")
        if not _TAG_VAR_VALUE.match(val):
            raise ValueError(f"invalid image reference for {name}: {val!r}")
        out[name] = val
    return out


def package_match_name(name: str) -> str:
    """Name used to match `Repo.ignored_packages`: case- and separator-
    insensitive, so `Foo_Bar` and `foo-bar` (PEP 503) are one package."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def clean_package_names(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    seen: set[str] = set()
    out: list[str] = []
    for raw in value:
        name = str(raw).strip()
        if name and package_match_name(name) not in seen:
            seen.add(package_match_name(name))
            out.append(name)
    return out


def clean_branches(value) -> list[str]:
    """Normalize a branch list: strip, dedupe (order-preserving), require at
    least one. A repo tracks each branch as an independent scan history —
    an empty list would mean nothing is ever scanned."""
    if value is None:
        raise ValueError("at least one branch is required")
    if isinstance(value, str):
        value = [value]
    seen: set[str] = set()
    out: list[str] = []
    for raw in value:
        b = str(raw).strip()
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    if not out:
        raise ValueError("at least one branch is required")
    return out


class Repo(BaseModel):
    id: str
    # "git": clone `url` per branch. "upload": scan the zip uploaded per
    # version (`branches` hold version labels; `url` is `upload://<file name>`).
    source: Literal["git", "upload"] = "git"
    url: str
    # Each branch is scanned and scheduled independently, with its own snapshot
    # history — lets one tracked project cover e.g. both `develop` and a release
    # branch without duplicating the repo entry.
    branches: list[str]
    release_date: date | None = None
    interval_days: int = 1
    # Optional Docker image whose installed OS packages (dpkg -l) are inventoried
    # each scan, e.g. "acme/platform:ui-6.1.0".
    inspect_image: str | None = None
    # Values for compose image-tag variables, e.g.
    # {"CORE_TAG": "acme/platform-dev:core-latest"} resolves
    # `image: index.docker.io/${CORE_TAG}` so that image is tracked + inventoried.
    # Shared across all branches of the repo (the compose files rarely differ).
    image_tags: dict[str, str] = Field(default_factory=dict)
    # Image-tag variables the last scan found in the repo's compose files —
    # server-maintained, read-only. Lets the UI show exactly which tags this repo
    # wants without re-cloning.
    detected_tag_vars: list[str] = Field(default_factory=list)
    token_override: str | None = None  # masked in API responses
    # Package names flagged IGNORED instead of evaluated (all branches), e.g.
    # test-only devDependencies. Matched by `package_match_name`.
    ignored_packages: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    # Per-branch last-scan timestamp — server-maintained.
    last_scan_by_branch: dict[str, datetime] = Field(default_factory=dict)

    @field_validator("branches", mode="before")
    @classmethod
    def _clean_branches(cls, v) -> list[str]:
        return clean_branches(v)

    @field_validator("image_tags", mode="before")
    @classmethod
    def _clean_image_tags(cls, v) -> dict[str, str]:
        return clean_image_tags(v)

    @field_validator("ignored_packages", mode="before")
    @classmethod
    def _clean_ignored(cls, v) -> list[str]:
        return clean_package_names(v)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data):
        if not isinstance(data, dict):
            return data
        # accept older repos.json entries that stored interval_hours
        if "interval_days" not in data and "interval_hours" in data:
            hours = data.get("interval_hours") or 24
            data = {**data, "interval_days": max(1, round(hours / 24))}
        # accept older repos.json entries with a single `branch` string
        if "branches" not in data and "branch" in data:
            data = {**data, "branches": [data["branch"]]}
        # accept older repos.json entries with a single `last_scan` timestamp —
        # attribute it to the (sole, pre-migration) legacy branch
        if "last_scan_by_branch" not in data and data.get("last_scan"):
            legacy_branches = data.get("branches") or []
            if legacy_branches:
                data = {**data, "last_scan_by_branch": {legacy_branches[0]: data["last_scan"]}}
        return data

    @field_validator("interval_days")
    @classmethod
    def _positive_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("interval_days must be >= 1")
        return v


class RepoCreate(BaseModel):
    url: str
    branches: list[str]
    release_date: date | None = None
    interval_days: int = 1
    inspect_image: str | None = None
    image_tags: dict[str, str] = Field(default_factory=dict)
    token_override: str | None = None
    ignored_packages: list[str] = Field(default_factory=list)
    id: str | None = None  # server derives from url if omitted

    @field_validator("branches", mode="before")
    @classmethod
    def _clean_branches(cls, v) -> list[str]:
        return clean_branches(v)

    @field_validator("image_tags", mode="before")
    @classmethod
    def _clean_image_tags(cls, v) -> dict[str, str]:
        return clean_image_tags(v)

    @field_validator("ignored_packages", mode="before")
    @classmethod
    def _clean_ignored(cls, v) -> list[str]:
        return clean_package_names(v)


class UploadRepoCreate(BaseModel):
    """Settings sent alongside the zip when adding an upload repo."""
    name: str                      # repo id is derived from it
    version: str | None = None     # label for this zip; "upload" when omitted
    release_date: date | None = None
    interval_days: int = 1
    image_tags: dict[str, str] = Field(default_factory=dict)
    ignored_packages: list[str] = Field(default_factory=list)

    @field_validator("image_tags", mode="before")
    @classmethod
    def _clean_image_tags(cls, v) -> dict[str, str]:
        return clean_image_tags(v)

    @field_validator("ignored_packages", mode="before")
    @classmethod
    def _clean_ignored(cls, v) -> list[str]:
        return clean_package_names(v)


DEFAULT_UPLOAD_VERSION = "upload"


def upload_version(label: str | None) -> str:
    return (label or "").strip() or DEFAULT_UPLOAD_VERSION


class RepoUpdate(BaseModel):
    branches: list[str] | None = None
    release_date: date | None = None
    interval_days: int | None = None
    inspect_image: str | None = None
    image_tags: dict[str, str] | None = None
    token_override: str | None = None
    ignored_packages: list[str] | None = None

    @field_validator("branches", mode="before")
    @classmethod
    def _clean_branches(cls, v) -> list[str] | None:
        return None if v is None else clean_branches(v)

    @field_validator("ignored_packages", mode="before")
    @classmethod
    def _clean_ignored(cls, v) -> list[str] | None:
        return None if v is None else clean_package_names(v)

    @field_validator("image_tags", mode="before")
    @classmethod
    def _clean_image_tags(cls, v) -> dict[str, str] | None:
        return None if v is None else clean_image_tags(v)


# --------------------------------------------------------------------------- #
# Snapshots & scan status                                                      #
# --------------------------------------------------------------------------- #
class SnapshotSummary(BaseModel):
    ts: str  # UTC timestamp folder name, e.g. 2026-07-13T10-30-00Z
    package_count: int = 0
    flag_counts: dict[str, int] = Field(default_factory=dict)
    degraded: bool = False


class ScanState(str, Enum):
    idle = "idle"
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"


class ScanIssue(BaseModel):
    """One thing that went wrong during a scan, kept per-stage so the UI can say
    *what* degraded rather than just *that* something did."""
    stage: str        # clone | extract | inspect | enrich | flag | snapshot | scan
    message: str
    fatal: bool = False   # True only for the last-resort guard that aborts the run


class ScanStatus(BaseModel):
    repo_id: str
    branch: str = ""
    state: ScanState = ScanState.idle
    phase: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str = ""
    snapshot_ts: str | None = None
    # A degraded run still produces a snapshot — it's just missing data. Every
    # issue is kept (not overwritten) so the UI can list all of them; `message`
    # stays as the single-line summary for compact call sites.
    degraded: bool = False
    issues: list[ScanIssue] = Field(default_factory=list)


class LogEntry(BaseModel):
    """One line in a scan's activity trace."""
    ts: datetime
    stage: str        # clone | extract | enrich | flag | snapshot | agent | source | scan
    level: str = "info"   # info | warn | error | tool | result
    message: str
    data: dict | None = None  # tool name/input/output, counts, samples…


class ScanLog(BaseModel):
    repo_id: str
    branch: str = ""
    snapshot_ts: str | None = None
    state: ScanState = ScanState.idle
    entries: list[LogEntry] = Field(default_factory=list)
