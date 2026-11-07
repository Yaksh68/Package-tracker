import datetime as dt

import app.agents.enrichment_agent as ea
from app.agents import source_apis as sa
from app.flags import evaluate
from app.models import Ecosystem, Enrichment, FlagStatus, Package, Verdict

# Verbatim table from github.com/benoitc/gunicorn/security (SECURITY.md).
GUNICORN = """\
## Supported Releases

| Version | Status             |
| ------- | ------------------ |
| 26.0.0  | :white_check_mark: |
| 25.3.0  | :white_check_mark: |
| 24.1.1  | :x:                |
| 23.0.0  | :x:                |
| 22.0.0  | :x:                |
| < 22.0  | :x:                |
"""


def _decide(md, version):
    return sa.security_support_for(sa.parse_security_policy(md), version)


def test_gunicorn_release_lines():
    assert _decide(GUNICORN, "26.2.0") == (True, "26.0.0")
    assert _decide(GUNICORN, "25.1.0") == (True, "25.3.0")
    assert _decide(GUNICORN, "24.1.1") == (False, "24.1.1")
    assert _decide(GUNICORN, "23.0.0") == (False, "23.0.0")
    assert _decide(GUNICORN, "20.1.0") == (False, "< 22.0")
    assert _decide(GUNICORN, "27.0.0") == (True, "26.0.0")  # newer than any row


def test_wildcards_words_and_emoji():
    md = """
| Version | Supported          |
|---------|--------------------|
| 5.1.x   | ✅ |
| 5.0.x   | ❌ |
| 4.*     | Not supported |
| >= 6.0  | Yes |
"""
    assert _decide(md, "5.1.3") == (True, "5.1.x")
    assert _decide(md, "5.0.9") == (False, "5.0.x")
    assert _decide(md, "4.2") == (False, "4.*")
    assert _decide(md, "6.1") == (True, ">= 6.0")
    assert _decide(md, "3.0") is None  # not covered → no claim


def test_zero_major_tie_break():
    md = "| Version | Supported |\n|--|--|\n| 0.9 | ✅ |\n| 0.8 | ❌ |\n"
    assert _decide(md, "0.8.3") == (False, "0.8")
    assert _decide(md, "0.9.1") == (True, "0.9")
    assert _decide(md, "0.7.0") == (False, "0.8")


def test_no_table_or_bad_version():
    assert sa.parse_security_policy("Email security@example.com") == []
    assert _decide(GUNICORN, None) is None
    assert _decide(GUNICORN, "not-a-version") is None


def test_github_slug_handles_subpages():
    assert sa.github_slug("https://github.com/benoitc/gunicorn/issues") == "benoitc/gunicorn"
    assert sa.github_slug("git+https://github.com/a/b.git") == "a/b"
    assert sa.github_slug("https://github.com/sponsors/someone") is None
    # PyPI gunicorn: issue tracker comes first, source code must still win
    urls = {"Issue tracker": "https://github.com/benoitc/gunicorn/issues",
            "Source code": "https://github.com/benoitc/gunicorn",
            "Homepage": "https://gunicorn.org"}
    assert sa.pick_github_repo(urls) == "https://github.com/benoitc/gunicorn"
    assert sa._npm_github_repo({"type": "git", "url": "git+https://github.com/x/y.git"}) == "https://github.com/x/y"
    assert sa._npm_github_repo("github:x/y") == "https://github.com/x/y"
    assert sa._npm_github_repo("x/y") == "https://github.com/x/y"


def _pkg(v):
    return Package(name="gunicorn", version=v, ecosystem=Ecosystem.pypi, source_file="requirements.txt")


def test_unsupported_policy_flags_already_eol():
    e = Enrichment(name="gunicorn", version="24.1.1", ecosystem=Ecosystem.pypi,
                   verdict=Verdict.supported)
    ea._apply_security_policy(e, {"found": True, "supported": False, "row": "24.1.1",
                                  "url": "https://github.com/benoitc/gunicorn/security/policy"}, e.sources)
    assert e.verdict == Verdict.eol and e.security_supported is False
    assert not ea._needs_ai(e, stale_years=2)
    f = evaluate(_pkg("24.1.1"), e, None, today=dt.date(2026, 10, 4))
    assert f.status == FlagStatus.ALREADY_EOL
    assert f.reasons == ["upstream SECURITY.md lists '24.1.1' as unsupported"]


def test_legacy_advisory_data_still_loads():
    # snapshots/backups written while advisories were tracked carry these keys
    e = Enrichment.model_validate({"name": "gunicorn", "version": "21.2.0", "ecosystem": "pypi",
                                   "advisories": [{"id": "GHSA-a", "severity": "high"}]})
    assert not hasattr(e, "advisories")
    assert evaluate(_pkg("21.2.0"), e, None, today=dt.date(2026, 10, 4)).status != FlagStatus.VULNERABLE


def test_supported_policy_stays_ok():
    e = Enrichment(name="gunicorn", version="26.2.0", ecosystem=Ecosystem.pypi,
                   verdict=Verdict.supported, last_release=dt.date(2026, 8, 24))
    ea._apply_security_policy(e, {"found": True, "supported": True, "row": "26.0.0",
                                  "url": "u"}, e.sources)
    assert e.security_supported is True
    assert evaluate(_pkg("26.2.0"), e, None, today=dt.date(2026, 10, 4)).status == FlagStatus.OK


def test_ai_cannot_overrule_unsupported_policy():
    from app.models import AiVerdict
    e = Enrichment(name="gunicorn", version="24.1.1", ecosystem=Ecosystem.pypi,
                   verdict=Verdict.eol, security_supported=False)
    ea._merge_ai(e, AiVerdict(name="gunicorn", ecosystem=Ecosystem.pypi, verdict=Verdict.supported))
    assert e.verdict == Verdict.eol
