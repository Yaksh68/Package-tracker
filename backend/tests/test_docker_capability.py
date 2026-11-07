"""Docker is a capability, not a requirement (phase 0, task 0.4).

Without Docker, the image inventory and the Docker-based apt version fallback
are skipped with a note in the scan log, and the scan isn't marked degraded.
With IMAGE_INSPECT=on, a missing Docker still degrades the scan, as before.
"""
import pytest

from app import pipeline, storage
from app.agents import apt_index, image_inspect
from app.agents.clone import Clone
from app.agents.image_inspect import ImageInspectError
from app.config import get_settings
from app.models import Ecosystem, Package, PackageList, Repo


@pytest.fixture(autouse=True)
def _fresh_probe(monkeypatch):
    monkeypatch.setattr(image_inspect, "_docker_probe", None)


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# inspect_mode / docker_available                                             #
# --------------------------------------------------------------------------- #
def test_auto_follows_docker_availability(monkeypatch):
    _settings(monkeypatch, IMAGE_INSPECT="auto", AUTH_MODE="none")
    monkeypatch.setattr(image_inspect, "_probe_docker", lambda: False)
    assert image_inspect.inspect_mode() == (False, "Docker isn't available on this host")
    monkeypatch.setattr(image_inspect, "_docker_probe", None)
    monkeypatch.setattr(image_inspect, "_probe_docker", lambda: True)
    assert image_inspect.inspect_mode()[0] is True


def test_on_and_off_override_detection(monkeypatch):
    monkeypatch.setattr(image_inspect, "_probe_docker", lambda: False)
    _settings(monkeypatch, IMAGE_INSPECT="on", AUTH_MODE="none")
    assert image_inspect.inspect_mode()[0] is True
    monkeypatch.setattr(image_inspect, "_probe_docker", lambda: True)
    _settings(monkeypatch, IMAGE_INSPECT="off")
    assert image_inspect.inspect_mode()[0] is False


def test_the_public_mode_never_uses_docker(monkeypatch):
    monkeypatch.setattr(image_inspect, "_probe_docker", lambda: True)
    _settings(monkeypatch, IMAGE_INSPECT="on", AUTH_MODE="github")
    enabled, why = image_inspect.inspect_mode()
    assert enabled is False and "public mode" in why


def test_the_docker_probe_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(image_inspect, "_probe_docker", lambda: calls.append(1) or True)
    assert image_inspect.docker_available() and image_inspect.docker_available()
    assert len(calls) == 1


def test_no_docker_binary_means_not_available(monkeypatch):
    monkeypatch.setattr(image_inspect.shutil, "which", lambda name: None)
    assert image_inspect._probe_docker() is False


# --------------------------------------------------------------------------- #
# The pipeline                                                                 #
# --------------------------------------------------------------------------- #
@pytest.fixture
def scan(monkeypatch, tmp_path):
    """Run a scan over an empty clone with no network; returns (status, log)."""
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(pipeline, "shallow_clone",
                        lambda url, branch, token=None: Clone(path=tmp_path / "repo",
                                                              _tmp=str(tmp_path / "unused")))
    monkeypatch.setattr(Clone, "cleanup", lambda self: None)

    async def no_enrich(packages, **kw):
        return []

    monkeypatch.setattr(pipeline.enrichment_agent, "enrich", no_enrich)

    async def run(repo_id: str):
        repo = Repo(id=repo_id, url="https://example.com/r.git", branches=["main"])
        status = await pipeline.run_scan(repo, "main", use_extraction_agent=False)
        log = storage.read_scan_log(repo_id, "main", storage.load_index(repo_id, "main")[0].ts)
        return status, [e["message"] for e in log["entries"]]

    return run


def _docker(monkeypatch, enabled: bool, why: str = "Docker isn't available on this host"):
    monkeypatch.setattr(image_inspect, "inspect_mode", lambda: (enabled, why))


async def test_no_docker_skips_the_image_inventory_without_degrading(monkeypatch, scan):
    _docker(monkeypatch, False)
    monkeypatch.setattr(image_inspect, "detect_images", lambda path, tags=None: ["ubuntu:24.04"])

    async def must_not_run(image, **kw):
        raise AssertionError("docker must not be called")

    monkeypatch.setattr(image_inspect, "list_image_packages", must_not_run)
    status, messages = await scan("no-docker-inventory")

    assert status.state.value == "done" and status.degraded is False
    assert any("Skipped the OS-package inventory of 1 Docker image(s)" in m for m in messages)


async def test_no_docker_leaves_unpinned_apt_packages_without_degrading(monkeypatch, scan):
    _docker(monkeypatch, False)

    async def extract(clone_path, *, use_agent=False, tag_vars=None):
        return PackageList(packages=[
            Package(name="curl", version=None, ecosystem=Ecosystem.apt, source_file="Dockerfile"),
        ])

    async def no_indexes(*a, **kw):
        return {}

    async def must_not_run(*a, **kw):
        raise AssertionError("docker must not be called")

    monkeypatch.setattr(pipeline.extraction_agent, "extract", extract)
    monkeypatch.setattr(apt_index, "indexes_for_sources", no_indexes)
    monkeypatch.setattr(image_inspect, "reconcile_apt_versions", must_not_run)
    status, messages = await scan("no-docker-apt")

    assert status.degraded is False
    assert any("1 apt package(s) stay without a version" in m for m in messages)


async def test_forcing_docker_on_still_degrades_when_it_fails(monkeypatch, scan):
    _docker(monkeypatch, True, "IMAGE_INSPECT=on")
    monkeypatch.setattr(image_inspect, "detect_images", lambda path, tags=None: ["ubuntu:24.04"])

    async def no_docker(image, **kw):
        raise ImageInspectError("docker is not installed / not on PATH")

    monkeypatch.setattr(image_inspect, "list_image_packages", no_docker)
    status, _ = await scan("docker-forced")

    assert status.degraded is True
    assert [i.stage for i in status.issues] == ["inspect"]
