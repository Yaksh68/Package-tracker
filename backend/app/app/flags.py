"""Flagging engine — pure, deterministic, release-date aware. No AI.

`evaluate` runs the severity ladder (first match sets status, but every
applicable reason is recorded). Re-running this against stored enrichment.json
is what makes "edit the release date → re-flag instantly, no re-scan" work.
"""
from __future__ import annotations

import calendar
from datetime import date

from .config import get_settings
from .models import Enrichment, Flag, FlagStatus, Package, Verdict, package_match_name


def newer_available(current: str | None, latest: str | None) -> bool:
    """True when `latest` is a strictly newer version than `current`. Uses PEP 440
    parsing with a plain string-inequality fallback for non-conforming schemes.
    Unknown current or latest → False (no provable upgrade path)."""
    if not current or not latest:
        return False
    try:
        from packaging.version import InvalidVersion, Version
        try:
            return Version(latest) > Version(current)
        except InvalidVersion:
            return latest != current
    except ImportError:  # pragma: no cover
        return latest != current


def upgrade_available(pkg: Package, enrichment: Enrichment | None) -> bool:
    """True when a newer **stable** release exists for `pkg`.

    Deliberately stricter than `newer_available`: both versions must parse as
    real versions — there is NO string-inequality fallback, because an
    unparseable pair proves nothing. That strictness is what keeps OS packages
    quiet: apt carries Debian-style versions (`3.12.3-1ubuntu0.15`) that don't
    parse, and a fallback would mark essentially every one of the hundreds of
    image-inventory rows as upgradable.

    Informational only — this is not part of the flag severity ladder.
    """
    latest = enrichment.latest_stable_version if enrichment else None
    if not pkg.version or not latest:
        return False
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover
        return False
    try:
        return Version(latest) > Version(pkg.version)
    except InvalidVersion:
        return False


def add_months(d: date, months: int) -> date:
    """Add calendar months, clamping the day to the target month's length."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def evaluate(
    pkg: Package,
    enrichment: Enrichment | None,
    release_date: date | None,
    *,
    today: date | None = None,
    soon_months: int | None = None,
    stale_years: int | None = None,
) -> Flag:
    settings = get_settings()
    today = today or date.today()
    soon_months = soon_months if soon_months is not None else settings.eol_soon_months
    stale_years = stale_years if stale_years is not None else settings.stale_years

    reasons: list[str] = []
    status: FlagStatus | None = None

    def mark(s: FlagStatus, reason: str) -> None:
        nonlocal status
        if status is None:
            status = s
        reasons.append(reason)

    e = enrichment
    eol = e.eol_date if e else None

    # 1. ALREADY_EOL
    if eol and eol <= today:
        mark(FlagStatus.ALREADY_EOL, f"EOL {eol.isoformat()} is on/before today")

    # 1b. Upstream SECURITY.md marks this release line unsupported — EOL, just
    #     without a published date.
    if e and e.security_supported is False:
        mark(
            FlagStatus.ALREADY_EOL,
            f"upstream SECURITY.md lists '{e.security_policy_row}' as unsupported",
        )

    # 2 & 3. Release-relative windows.
    if release_date:
        # 2. EOL_BEFORE_RELEASE (core requirement): EOL on/before the planned release
        if eol and today < eol <= release_date:
            mark(
                FlagStatus.EOL_BEFORE_RELEASE,
                f"EOL {eol.isoformat()} falls on/before release {release_date.isoformat()}",
            )
        # 3. EOL_SOON_AFTER_RELEASE: within N months after release
        soon_cutoff = add_months(release_date, soon_months)
        if eol and release_date < eol <= soon_cutoff:
            mark(
                FlagStatus.EOL_SOON_AFTER_RELEASE,
                f"EOL {eol.isoformat()} within {soon_months} months after release",
            )
    else:
        # No release date → rules 2–3 collapse to "EOL within N months from today".
        horizon = add_months(today, soon_months)
        if eol and today < eol <= horizon:
            mark(
                FlagStatus.EOL_BEFORE_RELEASE,
                f"EOL {eol.isoformat()} within {soon_months} months from today",
            )

    # 4. DEPRECATED — only when there is somewhere to go: a newer version to
    #    upgrade to, or a named successor to migrate to. A deprecated verdict with
    #    neither (current == latest, no successor) means the project is simply
    #    dormant → STALE instead.
    if e and e.verdict == Verdict.deprecated:
        newer = newer_available(pkg.version, e.latest_version)
        if newer:
            mark(
                FlagStatus.DEPRECATED,
                f"deprecated; newer version available ({e.latest_version})",
            )
        elif e.successor:
            mark(
                FlagStatus.DEPRECATED,
                f"deprecated; superseded by {e.successor}",
            )
        else:
            mark(
                FlagStatus.STALE,
                "deprecated with no newer version or successor — project dormant",
            )

    # explicit EOL verdict with no usable date still counts as already-eol
    if e and e.verdict == Verdict.eol and not eol and e.security_supported is not False:
        mark(FlagStatus.ALREADY_EOL, "verdict: eol (no date)")

    # 5. ARCHIVED
    if e and e.archived:
        mark(FlagStatus.ARCHIVED, "source repository is archived")

    # (There is no EOS rule. `eos_date` is end-of-**security**-support, the same
    # milestone as `eol_date`, so rule 1 already covers it. The rule that used to
    # live here fired on end-of-*active*-support — the start of security-only
    # maintenance — and reported packages as unsupported years early.
    # `FlagStatus.EOS_PASSED` is kept in the enum so snapshots written before
    # this change still load.)

    # 6. STALE — no release in > stale_years
    if e and e.last_release and add_months(e.last_release, stale_years * 12) < today:
        mark(
            FlagStatus.STALE,
            f"no release since {e.last_release.isoformat()} (> {stale_years}y)",
        )

    # 7 / 8 fallbacks
    if status is None:
        # UNKNOWN only when we truly lack a signal; a concrete future EOL/EOS date
        # (all severe rules above already passed) means it's fine for now → OK.
        if e is None or (
            e.verdict == Verdict.unknown and not e.eol_date and not e.eos_date
        ):
            mark(FlagStatus.UNKNOWN, "no authoritative EOL signal")
        else:
            mark(FlagStatus.OK, "supported")

    return Flag(key=pkg.key, status=status or FlagStatus.UNKNOWN, reasons=reasons)


def evaluate_all(
    packages: list[Package],
    enrich_by_key: dict[str, Enrichment],
    release_date: date | None,
    ignored: list[str] | None = None,
    **kw,
) -> list[Flag]:
    """Flag every package; names on the repo's ignore list get IGNORED instead."""
    skip = {package_match_name(n) for n in ignored or []}
    return [
        Flag(key=p.key, status=FlagStatus.IGNORED, reasons=["on this repo's ignore list"])
        if package_match_name(p.name) in skip
        else evaluate(p, enrich_by_key.get(p.key), release_date, **kw)
        for p in packages
    ]


def count_flags(flags: list[Flag]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in flags:
        counts[f.status.value] = counts.get(f.status.value, 0) + 1
    return counts
