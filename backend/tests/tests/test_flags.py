from datetime import date

from app.models import (
    Confidence,
    Ecosystem,
    Enrichment,
    FlagStatus,
    Package,
    Verdict,
)
from app.flags import add_months, evaluate, upgrade_available

TODAY = date(2026, 7, 14)
RELEASE = date(2026, 9, 15)


def pkg(name="lib", eco=Ecosystem.pypi):
    return Package(name=name, version="1.0.0", ecosystem=eco, source_file="requirements.txt")


def enr(**kw):
    base = dict(name="lib", version="1.0.0", ecosystem=Ecosystem.pypi)
    base.update(kw)
    return Enrichment(**base)


def test_add_months_clamps_day():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_already_eol():
    f = evaluate(pkg(), enr(eol_date=date(2017, 1, 1), verdict=Verdict.eol), RELEASE, today=TODAY)
    assert f.status == FlagStatus.ALREADY_EOL


def test_eol_before_release_core_case():
    # release in Sept, EOL in Aug -> flagged today
    f = evaluate(pkg(), enr(eol_date=date(2026, 8, 30)), RELEASE, today=TODAY)
    assert f.status == FlagStatus.EOL_BEFORE_RELEASE


def test_eol_exactly_on_release_is_before():
    f = evaluate(pkg(), enr(eol_date=RELEASE), RELEASE, today=TODAY)
    assert f.status == FlagStatus.EOL_BEFORE_RELEASE


def test_eol_soon_after_release():
    f = evaluate(pkg(), enr(eol_date=date(2026, 11, 1)), RELEASE, today=TODAY, soon_months=6)
    assert f.status == FlagStatus.EOL_SOON_AFTER_RELEASE


def test_eol_far_after_release_is_ok():
    f = evaluate(pkg(), enr(eol_date=date(2028, 1, 1)), RELEASE, today=TODAY, soon_months=6)
    assert f.status == FlagStatus.OK


def test_no_release_date_uses_today_horizon():
    # EOL within 6 months of today, no release date
    f = evaluate(pkg(), enr(eol_date=date(2026, 9, 1)), None, today=TODAY, soon_months=6)
    assert f.status == FlagStatus.EOL_BEFORE_RELEASE


def test_deprecated_with_newer_version():
    # current 1.0.0 < latest 2.4.0 → upgrade path exists → DEPRECATED
    f = evaluate(pkg(), enr(verdict=Verdict.deprecated, latest_version="2.4.0"),
                 RELEASE, today=TODAY)
    assert f.status == FlagStatus.DEPRECATED
    assert any("newer version" in r for r in f.reasons)


def test_deprecated_with_successor_same_version():
    # current == latest but an explicit successor exists → migrate → DEPRECATED
    f = evaluate(pkg(), enr(verdict=Verdict.deprecated, latest_version="1.0.0",
                            successor="newlib"), RELEASE, today=TODAY)
    assert f.status == FlagStatus.DEPRECATED
    assert any("superseded" in r for r in f.reasons)


def test_deprecated_no_newer_no_successor_is_stale():
    # current == latest, nothing to move to → project dormant → STALE
    f = evaluate(pkg(), enr(verdict=Verdict.deprecated, latest_version="1.0.0"),
                 RELEASE, today=TODAY)
    assert f.status == FlagStatus.STALE
    assert any("dormant" in r for r in f.reasons)


def test_archived():
    f = evaluate(pkg(), enr(archived=True), RELEASE, today=TODAY)
    assert f.status == FlagStatus.ARCHIVED


def test_no_flag_from_eos_alone():
    """`eos_date` is end-of-**security**-support — the same milestone as
    `eol_date`, so rule 1 handles it and there is no separate EOS rule.

    The rule that used to live here fired on end-of-*active*-support, which is
    only the switch to security-only maintenance: it reported Ubuntu's
    `python3 3.12.3-0ubuntu2.1` as unsupported while it is patched until 2029."""
    future = date(2028, 10, 31)
    f = evaluate(pkg(), enr(eol_date=future, eos_date=future), RELEASE, today=TODAY)
    assert f.status == FlagStatus.OK

    # a past security-support date is ALREADY_EOL, via rule 1 and nothing else
    past = date(2025, 1, 1)
    f = evaluate(pkg(), enr(eol_date=past, eos_date=past), RELEASE, today=TODAY)
    assert f.status == FlagStatus.ALREADY_EOL
    assert f.reasons == ["EOL 2025-01-01 is on/before today"]


def test_stale():
    f = evaluate(pkg(), enr(last_release=date(2021, 1, 1), verdict=Verdict.supported),
                 RELEASE, today=TODAY, stale_years=2)
    assert f.status == FlagStatus.STALE


def test_unknown_no_enrichment():
    f = evaluate(pkg(), None, RELEASE, today=TODAY)
    assert f.status == FlagStatus.UNKNOWN


def test_ok_supported():
    f = evaluate(pkg(), enr(verdict=Verdict.supported, confidence=Confidence.high),
                 RELEASE, today=TODAY)
    assert f.status == FlagStatus.OK


def test_severity_already_eol_beats_deprecated():
    f = evaluate(pkg(), enr(eol_date=date(2017, 1, 1), verdict=Verdict.deprecated),
                 RELEASE, today=TODAY)
    assert f.status == FlagStatus.ALREADY_EOL
    # but all reasons are recorded
    assert any("deprecated" in r for r in f.reasons)


# --------------------------------------------------------------------------- #
# upgrade_available — informational, independent of the flag ladder           #
# --------------------------------------------------------------------------- #
def test_upgrade_available_for_a_behind_but_supported_package():
    """The whole point: an OK / non-EOL package still surfaces an upgrade."""
    p = pkg()  # version 1.0.0
    e = enr(verdict=Verdict.supported, latest_stable_version="1.2.0")
    assert upgrade_available(p, e) is True
    # …and it does NOT change the flag — that stays OK
    assert evaluate(p, e, RELEASE, today=TODAY).status == FlagStatus.OK


def test_upgrade_available_false_when_current_or_missing():
    p = pkg()
    assert upgrade_available(p, enr(latest_stable_version="1.0.0")) is False  # equal
    assert upgrade_available(p, enr(latest_stable_version="0.9.0")) is False  # older
    assert upgrade_available(p, enr(latest_stable_version=None)) is False     # unknown
    assert upgrade_available(p, None) is False                                # no enrichment
    versionless = Package(name="lib", version=None, ecosystem=Ecosystem.pypi,
                          source_file="requirements.txt")
    assert upgrade_available(versionless, enr(latest_stable_version="1.2.0")) is False


def test_upgrade_available_ignores_the_plain_latest_version():
    """Only `latest_stable_version` counts — a prerelease in `latest_version`
    must never be advertised as an available upgrade."""
    e = enr(latest_version="2.0.0rc1", latest_stable_version=None)
    assert upgrade_available(pkg(), e) is False


def test_upgrade_available_stays_quiet_for_debian_style_apt_versions():
    """Regression guard: Debian versions don't parse, and a string-inequality
    fallback would mark every one of the hundreds of image-inventory rows as
    upgradable. Unparseable → no claim."""
    apt = Package(name="python3.12", version="3.12.3-1ubuntu0.15",
                  ecosystem=Ecosystem.apt, source_file="image:ubuntu:24.04",
                  is_direct=False)
    e = enr(name="python3.12", ecosystem=Ecosystem.apt, latest_stable_version="3.12.11")
    assert upgrade_available(apt, e) is False
