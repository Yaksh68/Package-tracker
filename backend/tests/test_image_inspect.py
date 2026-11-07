import pytest

from app.agents import image_inspect
from app.agents.image_inspect import (
    ImageInspectError,
    detect_images,
    detect_tag_vars,
    parse_dpkg_output,
    reconcile_apt_versions,
    validate_image_ref,
)
from app.models import Ecosystem, Package


def test_parse_dpkg_output():
    text = (
        "adduser,3.118ubuntu5,all\n"
        "apt,2.4.14,amd64\n"
        "bsdutils,1:2.37.2-4ubuntu3.5,amd64\n"
        "adduser,3.118ubuntu5,all\n"   # duplicate → deduped
        "malformed-line\n"
        "\n"
    )
    pkgs = parse_dpkg_output(text, "ubuntu:22.04")
    got = {p.name: p for p in pkgs}
    assert len(pkgs) == 3
    assert got["apt"].version == "2.4.14"
    assert got["bsdutils"].version == "1:2.37.2-4ubuntu3.5"  # epoch kept
    assert all(p.ecosystem == Ecosystem.apt for p in pkgs)
    assert all(p.is_direct is False for p in pkgs)            # inventory, not declared
    assert got["apt"].source_file == "image:ubuntu:22.04"


def test_validate_image_ref():
    assert validate_image_ref(" ubuntu:22.04 ") == "ubuntu:22.04"
    assert validate_image_ref("acme/platform:ui-6.1.0")
    for bad in ("", "-rm", "--privileged", "img; rm -rf /", "img with spaces"):
        with pytest.raises(ImageInspectError):
            validate_image_ref(bad)


def test_detect_images_compose_first_then_dockerfile(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "version: '3'\n"
        "services:\n"
        "  ui:\n"
        "    image: acme/platform:ui-6.1.0\n"
        "  core:\n"
        "    image: acme/platform:core-6.1.0\n"
        "  db:\n"
        "    image: ${MONGO_IMAGE}\n"          # unresolved env var → skipped
    )
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.11-slim AS build\n"
        "RUN pip install foo\n"
        "FROM python:3.11-slim\n"             # real base image
        "COPY --from=build /app /app\n"
    )
    imgs = detect_images(tmp_path)
    # compose runtime images come first (deployed artifacts), then base images
    assert imgs == [
        "acme/platform:ui-6.1.0",
        "acme/platform:core-6.1.0",
        "python:3.11-slim",
    ]


def test_detect_images_skips_build_stages_and_dedups(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM node:20 AS builder\n"
        "FROM node:20\n"                      # same as a real base → deduped
        "FROM builder\n"                      # stage alias → skipped
        "FROM scratch\n"                      # scratch → skipped
    )
    assert detect_images(tmp_path) == ["node:20"]


def test_detect_images_ignores_vendored_and_git(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "docker-compose.yml").write_text(
        "services:\n  x:\n    image: evil:latest\n"
    )
    assert detect_images(tmp_path) == []


def test_detect_images_ignores_agent_tooling_dir(tmp_path):
    """`.claude/` holds dev-only agent tooling, not shipped components. Its
    compose file used to queue `integration-analyzer:latest` for dpkg inventory,
    which failed the pull (it's built locally, never pushed)."""
    tools = tmp_path / ".claude" / "tools" / "integration_analyzer"
    tools.mkdir(parents=True)
    (tools / "docker-compose.yml").write_text(
        "services:\n  analyzer:\n    build: .\n    image: integration-analyzer:latest\n"
    )
    (tools / "Dockerfile").write_text("FROM python:3.12-slim-bookworm\n")
    (tmp_path / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    assert detect_images(tmp_path) == ["ubuntu:24.04"]


def test_detect_images_skips_locally_built_compose_services(tmp_path):
    """Belt and braces for a build-only service outside `.claude/`."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  app:\n    build: .\n    image: myapp:latest\n"
        "  cache:\n    image: redis:7\n"
    )
    assert detect_images(tmp_path) == ["redis:7"]


def test_detect_images_none_found(tmp_path):
    (tmp_path / "README.md").write_text("no docker here")
    assert detect_images(tmp_path) == []


def _compose_repo(tmp_path):
    """A deployment compose repo: every image tag comes from a variable."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  rabbitmq:\n"
        "    image: index.docker.io/${RABBITMQ_TAG}\n"
        "  mongodb:\n"
        "    image: index.docker.io/${MONGO_TAG}\n"
        "  core:\n"
        "    image: index.docker.io/${CORE_TAG}\n"
    )
    return tmp_path


def test_detect_tag_vars_reports_variables_to_supply(tmp_path):
    assert detect_tag_vars(_compose_repo(tmp_path)) == [
        "RABBITMQ_TAG", "MONGO_TAG", "CORE_TAG",
    ]


def test_detect_images_uses_supplied_tags(tmp_path):
    root = _compose_repo(tmp_path)
    assert detect_images(root) == []           # no tags supplied → nothing inspectable
    imgs = detect_images(root, {
        "MONGO_TAG": "acme/platform:mongo-6.1.0-8.0.18",
        "CORE_TAG": "acme/platform-dev:core-latest",
    })
    # only the supplied ones, registry host kept (that's a real pullable ref)
    assert imgs == [
        "index.docker.io/acme/platform:mongo-6.1.0-8.0.18",
        "index.docker.io/acme/platform-dev:core-latest",
    ]
    for ref in imgs:
        assert validate_image_ref(ref) == ref   # safe to hand to docker run


def _apt(name, version, source_file="Dockerfile"):
    return Package(name=name, version=version, ecosystem=Ecosystem.apt,
                   source_file=source_file, is_direct=True)


def _base(name, tag, source_file="Dockerfile"):
    return Package(name=name, version=tag, ecosystem=Ecosystem.docker_image,
                   source_file=source_file, is_direct=True)


@pytest.mark.asyncio
async def test_reconcile_fills_unpinned_dockerfile_apt_versions(monkeypatch):
    async def fake_resolve(image, names, *, timeout=180.0):
        assert image == "ubuntu:24.04"
        assert set(names) == {"python3.12", "python3-pip"}
        return {"python3.12": "3.12.3-1ubuntu0.15", "python3-pip": "24.0+dfsg-1ubuntu1.3"}

    monkeypatch.setattr(image_inspect, "resolve_apt_versions", fake_resolve)
    pkgs = [
        _base("ubuntu", "24.04"),
        _apt("python3.12", None),
        _apt("python3-pip", None),
    ]
    out = {p.name: p for p in await reconcile_apt_versions(pkgs)}
    assert out["python3.12"].version == "3.12.3-1ubuntu0.15"
    assert out["python3-pip"].version == "24.0+dfsg-1ubuntu1.3"


@pytest.mark.asyncio
async def test_reconcile_leaves_pinned_and_unmapped_untouched(monkeypatch):
    async def fake_resolve(image, names, *, timeout=180.0):
        # only the version-less package mapped to a base image is queried
        assert names == ["curl"]
        return {}  # apt found nothing (offline / non-apt image) → degrade

    monkeypatch.setattr(image_inspect, "resolve_apt_versions", fake_resolve)
    pkgs = [
        _base("ubuntu", "24.04"),
        _apt("nginx", "1.28.0-1~jammy"),        # already pinned → skipped
        _apt("curl", None),                      # unpinned + mapped → queried
        _apt("openssl", None, source_file="image:mongo:6.0.12"),  # inventory src, no base → skipped
    ]
    out = {p.name: p for p in await reconcile_apt_versions(pkgs)}
    assert out["nginx"].version == "1.28.0-1~jammy"
    assert out["curl"].version is None       # unresolved, left as-is
    assert out["openssl"].version is None


@pytest.mark.asyncio
async def test_reconcile_noop_when_nothing_unpinned():
    pkgs = [_base("ubuntu", "24.04"), _apt("python3.12", "3.12.3-1ubuntu0.15")]
    out = await reconcile_apt_versions(pkgs)
    assert out == pkgs  # short-circuits, no docker call


def test_apt_excluded_from_ai_candidates():
    from app.agents.enrichment_agent import _needs_ai
    from app.models import Confidence, Enrichment, Verdict
    e = Enrichment(name="python3-pip", ecosystem=Ecosystem.apt,
                   verdict=Verdict.unknown, confidence=Confidence.low)
    assert _needs_ai(e, stale_years=2) is False
