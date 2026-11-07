"""apt packages the scan used to leave UNKNOWN: Dockerfile installs with no
version, Ubuntu builds without an `ubuntu` revision, virtual names, MongoDB's
split server packages and Microsoft's ODBC driver."""
import datetime as dt
import io

import httpx
import pytest

import app.agents.enrichment_agent as ea
from app.agents import apt_index, image_inspect, parsers, source_apis
from app.agents.apt_index import AptIndex, AptRepo, compare_versions, parse_packages
from app.config import get_settings
from app.models import Ecosystem, Package, Verdict

TODAY = dt.date(2026, 10, 26)
NOBLE = ("ubuntu", "24.04")

MAIN = """\
Package: curl
Version: 8.5.0-2ubuntu10

Package: apt
Version: 2.8.3

Package: libc6-dev
Version: 2.39-0ubuntu8.9
Provides: libc-dev (= 2.39-0ubuntu8.9), libc6-dev-x32-cross

Package: libdb5.3t64
Version: 5.3.28+dfsg2-7
"""
UPDATES = """\
Package: curl
Version: 8.5.0-2ubuntu10.16
"""
UNIVERSE = """\
Package: unixodbc
Version: 2.3.12-1ubuntu0.24.04.1
"""
MICROSOFT = """\
Package: msodbcsql18
Version: 18.6.2.1-1

Package: msodbcsql18
Version: 18.7.1.1-1

Package: docker-thing
Version: 1.0~ubuntu24.04
"""


def _index() -> AptIndex:
    idx = AptIndex(series="noble")
    parse_packages(MAIN, suite="noble", component="main", label="ubuntu 24.04", into=idx)
    parse_packages(UPDATES, suite="noble-updates", component="main", label="ubuntu 24.04", into=idx)
    parse_packages(UNIVERSE, suite="noble-updates", component="universe", label="ubuntu 24.04", into=idx)
    parse_packages(MICROSOFT, suite="noble", component="main", label="packages.microsoft.com", into=idx)
    return idx


def apt(name, version=None, source_file="Dockerfile"):
    return Package(name=name, version=version, ecosystem=Ecosystem.apt,
                   source_file=source_file, is_direct=True)


# --------------------------------------------------------------------------- #
# index                                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("a, b, sign", [
    ("1.0~rc1", "1.0", -1), ("1:2.0", "3.0", 1), ("2.43.0-1ubuntu7.4", "2.43.0-1ubuntu7.10", -1),
    ("1.7.1-3build1", "1.7.1-3ubuntu0.24.04.2", -1), ("1.0", "1.0-0", 0), ("1.0a", "1.0", 1),
])
def test_debian_version_order(a, b, sign):
    r = compare_versions(a, b)
    assert (r > 0) - (r < 0) == sign


def test_candidate_is_the_newest_across_pockets():
    hit = _index().candidate("curl")
    assert (hit.version, hit.suite) == ("8.5.0-2ubuntu10.16", "noble-updates")
    assert _index().candidate("msodbcsql18").version == "18.7.1.1-1"


def test_virtual_names_resolve_to_their_provider():
    hit = _index().candidate("libc-dev")
    assert (hit.name, hit.version, hit.provided_by) == ("libc6-dev", "2.39-0ubuntu8.9", "libc6-dev")
    assert _index().published("libc-dev", "2.39-0ubuntu8.9").provided_by == "libc6-dev"


def test_published_is_exact():
    idx = _index()
    assert idx.published("apt", "2.8.3").is_ubuntu
    assert idx.published("apt", "2.8.2") is None
    assert idx.published("nope", "1") is None


async def test_index_files_are_cached_compressed(monkeypatch, tmp_path):
    import lzma
    monkeypatch.setenv("APT_INDEX_CACHE_DIR", str(tmp_path))
    get_settings.cache_clear()
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url).endswith(".xz"):
            return httpx.Response(200, content=lzma.compress(MAIN.encode()))
        return httpx.Response(404)

    repo = AptRepo("http://archive.ubuntu.com/ubuntu", "noble", ("main",), "ubuntu 24.04")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        first = await apt_index.build_index(c, [repo])
        second = await apt_index.build_index(c, [repo])  # frozen release pocket: from disk
    assert first.candidate("apt").version == second.candidate("apt").version == "2.8.3"
    assert len(calls) == 1 and list(tmp_path.glob("*.xz"))


# --------------------------------------------------------------------------- #
# Dockerfiles                                                                  #
# --------------------------------------------------------------------------- #
MS_DOCKERFILE = """\
FROM ubuntu:24.04
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /k && \\
    echo "deb [arch=amd64,arm64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/ubuntu/24.04/prod noble main" > /etc/apt/sources.list.d/mssql-release.list && \\
    echo "deb ${MONGO_REPO}/apt/ubuntu noble/mongodb-org/8.0 multiverse" > /x && \\
    ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc
"""


def test_apt_sources_and_base_from_a_dockerfile():
    assert parsers.parse_apt_sources(MS_DOCKERFILE) == [
        ("https://packages.microsoft.com/ubuntu/24.04/prod", "noble", ("main",))]
    assert parsers.dockerfile_base("FROM ubuntu:24.04 AS build\nFROM build\nFROM ubuntu:24.04\n") == "ubuntu:24.04"


def test_every_dockerfile_keeps_its_own_base(tmp_path):
    """Three Dockerfiles on the same base dedupe to ONE base-image package; the
    context read from the files must still know all three."""
    for d in ("", "rabbitmq/", "mongodb/"):
        (tmp_path / d).mkdir(exist_ok=True)
        (tmp_path / d / "Dockerfile").write_text(MS_DOCKERFILE if not d else "FROM ubuntu:24.04\n")
    ctx = image_inspect.dockerfile_context(tmp_path)
    assert ctx.bases == {"Dockerfile": "ubuntu:24.04", "rabbitmq/Dockerfile": "ubuntu:24.04",
                         "mongodb/Dockerfile": "ubuntu:24.04"}
    assert list(ctx.apt_repos) == ["Dockerfile"]
    assert image_inspect.distro_by_dockerfile([], ctx.bases)["mongodb/Dockerfile"] == NOBLE


def test_resolve_unpinned_fills_versions():
    pkgs = [apt("curl"), apt("libc-dev"), apt("msodbcsql18"), apt("not-anywhere"), apt("zip", "3.0")]
    out, filled = apt_index.resolve_unpinned(pkgs, {"Dockerfile": _index()})
    assert [p.version for p in out] == ["8.5.0-2ubuntu10.16", "2.39-0ubuntu8.9", "18.7.1.1-1", None, "3.0"]
    assert filled["libc-dev"] == "2.39-0ubuntu8.9 (via libc6-dev)"


async def test_docker_fallback_drops_names_apt_cannot_find(monkeypatch):
    """One package apt doesn't know used to fail the whole batch (0/8 resolved)."""
    runs = []

    async def fake(image, names, timeout):
        runs.append(list(names))
        if "msodbcsql18" in names:
            return "", "E: Unable to locate package msodbcsql18\n"
        return "Inst curl (8.5.0-2ubuntu10.16 Ubuntu:24.04/noble-updates [amd64])\n", ""

    monkeypatch.setattr(image_inspect, "_simulate_install", fake)
    got = await image_inspect.resolve_apt_versions("ubuntu:24.04", ["curl", "msodbcsql18"])
    assert got == {"curl": "8.5.0-2ubuntu10.16"}
    assert runs == [["curl", "msodbcsql18"], ["curl"]]


# --------------------------------------------------------------------------- #
# enrichment                                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
def eol_api(monkeypatch):
    data = {"ubuntu": {"cycle": "24.04", "eol_date": dt.date(2029, 5, 31), "is_eol": False,
                       "url": "https://endoflife.date/ubuntu"},
            "mongodb": {"cycle": "8.3", "eol_date": dt.date(2029, 10, 31), "is_eol": False,
                        "url": "https://endoflife.date/mongodb"}}

    async def fake(client, name, version):
        slug = await source_apis.resolve_eol_slug(client, name) if name != "ubuntu" else "ubuntu"
        hit = data.get(slug or "")
        return {"matched": True, "slug": slug, **hit} if hit else {"matched": False}

    async def no_products(client):
        return []

    monkeypatch.setattr(source_apis, "endoflife_products", no_products)
    monkeypatch.setattr(ea.source_apis, "query_endoflife", fake)


@pytest.fixture
def no_launchpad(monkeypatch):
    calls = []

    async def fake(client, codename, name, version, *, label):
        calls.append((name, version))
        return None

    monkeypatch.setattr(apt_index, "launchpad_published", fake)
    return calls


async def _enrich(pkg, index=None):
    return await ea._enrich_one(None, pkg, today=TODAY, distro=NOBLE, index=index or _index())


async def test_ubuntu_build_without_revision_gets_the_distro_window(eol_api, no_launchpad):
    e = await _enrich(apt("apt", "2.8.3", source_file="image:mongo:8.3.2"))
    assert e.eol_date == dt.date(2029, 5, 31) and e.verdict == Verdict.supported
    assert any("noble/main" in s.note for s in e.sources)
    assert no_launchpad == []  # found in the index; no Launchpad call


async def test_superseded_version_is_checked_on_launchpad(eol_api, monkeypatch):
    async def lp(client, codename, name, version, *, label):
        return apt_index.Hit(name, version, "noble-updates", "main", label)

    monkeypatch.setattr(apt_index, "launchpad_published", lp)
    e = await _enrich(apt("apt", "2.8.1", source_file="image:mongo:8.3.2"))
    assert e.eol_date == dt.date(2029, 5, 31)


async def test_universe_is_flagged_as_community_maintained(eol_api, no_launchpad):
    e = await _enrich(apt("unixodbc", "2.3.12-1ubuntu0.24.04.1"))
    assert e.eol_date == dt.date(2029, 5, 31)
    assert "universe" in e.summary and "Ubuntu Pro" in e.summary


async def test_virtual_package_notes_its_provider(eol_api, no_launchpad):
    e = await _enrich(apt("libc-dev", "2.39-0ubuntu8.9"))
    assert e.eol_date == dt.date(2029, 5, 31) and "Installed as libc6-dev" in e.summary


async def test_third_party_repo_is_not_ubuntu_even_with_ubuntu_in_its_version(eol_api, no_launchpad):
    e = await _enrich(apt("docker-thing", "1.0~ubuntu24.04"))
    assert e.eol_date is None and e.verdict == Verdict.unknown


async def test_msodbcsql18_takes_microsofts_lifecycle(eol_api, no_launchpad, monkeypatch):
    async def ms(client, name):
        return ({"matched": True, "product": "ODBC Driver 18 for SQL Server",
                 "eol_date": dt.date(2028, 1, 11), "url": source_apis.MSODBC_LIFECYCLE_URL}
                if name == "msodbcsql18" else {"matched": False})

    monkeypatch.setattr(source_apis, "query_vendor_lifecycle", ms)
    e = await _enrich(apt("msodbcsql18", "18.6.2.1-1"))
    assert (e.eol_date, e.verdict, e.latest_version) == (dt.date(2028, 1, 11), Verdict.supported, "18.7.1.1-1")


async def test_mongodb_server_packages_share_the_server_cycle(eol_api, no_launchpad):
    e = await _enrich(apt("mongodb-org-shell", "8.3.2", source_file="image:mongo:8.3.2"), index=AptIndex())
    assert e.eol_date == dt.date(2029, 10, 31)
    e = await _enrich(apt("mongodb-mongosh", "2.8.3", source_file="image:mongo:8.3.2"), index=AptIndex())
    assert e.eol_date is None  # MongoDB publishes no EOL for mongosh


MS_TABLE = """<table><tbody>
<tr><td>ODBC Driver 18 for SQL Server</td><td>January 11, 2028<sup>1</sup></td><td>Not applicable</td></tr>
<tr><td>ODBC Driver 17 for SQL Server<sup>2</sup></td><td>January 11, 2028</td><td>January 11, 2033</td></tr>
</tbody></table>"""


def test_microsoft_lifecycle_table_parses():
    assert source_apis.parse_msodbc_lifecycle(MS_TABLE) == {
        "18": dt.date(2028, 1, 11), "17": dt.date(2028, 1, 11)}
