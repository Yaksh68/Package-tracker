"""OS packages are maintained by their distribution, not by the upstream project.

`python3 3.12.3-0ubuntu2.1` is Canonical's build of CPython 3.12 for Ubuntu
24.04: Canonical backports fixes until 2029-05-31, long past upstream's own
schedule. Matching such a package to the upstream release train by *name* is
what reported `perl 5.38.2-3.2ubuntu0.3` as ALREADY_EOL and `python3` as
support-ended, both of which are supported for years yet.
"""
import datetime as dt

import pytest

import app.agents.enrichment_agent as ea
from app.agents import image_inspect
from app.models import Confidence, Ecosystem, Package, Verdict

TODAY = dt.date(2026, 8, 10)
UBUNTU_EOL = dt.date(2029, 5, 31)


def apt(name, version, source_file="image:ubuntu:24.04"):
    return Package(name=name, version=version, ecosystem=Ecosystem.apt,
                   source_file=source_file, is_direct=False)


@pytest.fixture
def eol_api(monkeypatch):
    """endoflife.date stubbed with the real shapes for the products involved."""
    seen: list[tuple[str, str | None]] = []
    data = {
        "ubuntu": {"cycle": "24.04", "eol_date": UBUNTU_EOL, "is_eol": False,
                   "url": "https://endoflife.date/ubuntu"},
        "python": {"cycle": "3.12", "eol_date": dt.date(2028, 10, 31), "is_eol": False,
                   "url": "https://endoflife.date/python"},
        "perl": {"cycle": "5.38", "eol_date": dt.date(2026, 7, 2), "is_eol": True,
                 "url": "https://endoflife.date/perl"},
        "mongodb": {"cycle": "8.3", "eol_date": dt.date(2029, 10, 31), "is_eol": False,
                    "url": "https://endoflife.date/mongodb"},
    }
    aliases = {"python3": "python", "python3.12": "python", "mongodb-org": "mongodb"}

    async def fake_query(client, name, version):
        seen.append((name, version))
        slug = aliases.get(name.lower(), name.lower())
        hit = data.get(slug)
        return {"matched": True, "slug": slug, **hit} if hit else {"matched": False}

    monkeypatch.setattr(ea.source_apis, "query_endoflife", fake_query)
    return seen


async def enrich_one(pkg, distro=None):
    return await ea._enrich_one(None, pkg, today=TODAY, distro=distro)


async def test_ubuntu_package_takes_the_distro_window(eol_api):
    """The reported bug: upstream CPython's schedule applied to Ubuntu's build."""
    e = await enrich_one(apt("python3", "3.12.3-0ubuntu2.1"), distro=("ubuntu", "24.04"))

    assert e.eol_date == UBUNTU_EOL
    assert e.verdict == Verdict.supported
    assert e.confidence == Confidence.high
    # endoflife.date was asked about ubuntu 24.04 — never about python
    assert eol_api == [("ubuntu", "24.04")]
    assert "endoflife.date/ubuntu" in e.sources[0].url


async def test_ubuntu_perl_is_not_already_eol(eol_api):
    """upstream perl 5.38 is EOL; Ubuntu 24.04's build of it is not."""
    e = await enrich_one(apt("perl", "5.38.2-3.2ubuntu0.3"), distro=("ubuntu", "24.04"))
    assert e.verdict == Verdict.supported
    assert e.eol_date == UBUNTU_EOL


async def test_third_party_apt_package_keeps_its_upstream_window(eol_api):
    """`mongodb-org 8.3.4` comes from MongoDB's own apt repo with a pristine
    upstream version — no distro rebuilt it, so upstream's cycle is correct."""
    e = await enrich_one(apt("mongodb-org", "8.3.4"), distro=("ubuntu", "24.04"))
    assert e.eol_date == dt.date(2029, 10, 31)
    assert eol_api == [("mongodb-org", "8.3.4")]


async def test_no_known_distro_asserts_nothing(eol_api):
    """Better an unknown than an upstream date that doesn't apply."""
    e = await enrich_one(apt("python3", "3.12.3-0ubuntu2.1"), distro=None)
    assert e.eol_date is None
    assert e.verdict == Verdict.unknown
    assert eol_api == []


async def test_eos_mirrors_end_of_security_support(eol_api):
    """EOS is end-of-security-support, the same milestone as EOL — never the
    end-of-active-support date that used to land here."""
    e = await enrich_one(apt("python3", "3.12.3-0ubuntu2.1"), distro=("ubuntu", "24.04"))
    assert e.eos_date == e.eol_date == UBUNTU_EOL


@pytest.mark.parametrize("version, expected", [
    ("3.12.3-0ubuntu2.1", True),
    ("1:2.39.3-9ubuntu6.5", True),      # epoch prefix
    ("0.8.2-2build1", True),
    ("20260601~24.04.1", True),         # backport suffix, no revision
    ("1.2.3-1~deb12u2", True),
    ("8.3.4", False),                   # third-party repo, pristine upstream
    ("2.8.3", False),                   # native package, no revision
])
def test_distro_revision_detection(version, expected):
    assert ea.is_distro_build(apt("x", version)) is expected


def test_distro_build_only_applies_to_apt():
    pkg = Package(name="x", version="1.0-1ubuntu2", ecosystem=Ecosystem.pypi,
                  source_file="requirements.txt")
    assert ea.is_distro_build(pkg) is False


def test_os_release_parsing():
    text = (
        'PRETTY_NAME="Ubuntu 24.04.3 LTS"\n'
        'NAME="Ubuntu"\n'
        "VERSION_ID=\"24.04\"\n"
        "ID=ubuntu\n"
        "ID_LIKE=debian\n"
    )
    assert image_inspect.parse_os_release(text) == ("ubuntu", "24.04")


def test_os_release_without_version_is_unusable():
    assert image_inspect.parse_os_release("ID=scratch\n") is None


def test_dockerfile_base_image_supplies_the_distro():
    """A Dockerfile's own apt installs need no image inventory — `FROM
    ubuntu:24.04` already names the distribution."""
    pkgs = [
        Package(name="ubuntu", version="24.04", ecosystem=Ecosystem.docker_image,
                source_file="Dockerfile"),
        Package(name="node", version="20", ecosystem=Ecosystem.docker_image,
                source_file="ui/Dockerfile"),  # not a distro base image
    ]
    assert image_inspect.distro_by_dockerfile(pkgs) == {"Dockerfile": ("ubuntu", "24.04")}
