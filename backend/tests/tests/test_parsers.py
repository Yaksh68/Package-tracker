from app.agents import parsers
from app.agents.extraction_agent import extract_vendored, walk_manifests
from app.models import Ecosystem


def test_manifest_walk_skips_agent_tooling_and_vendored_dirs():
    """`.claude/` is dev-only agent tooling — its Dockerfile/pyproject are not
    part of the product's dependency surface and must not reach the SBOM."""
    assert {".claude", ".git", "node_modules", "vendor"} <= parsers.SKIP_DIRS
    assert parsers.under_skipped((".claude", "tools", "x", "Dockerfile"))
    assert not parsers.under_skipped(("core", "requirements.txt"))


def test_walk_manifests_excludes_skipped_dirs(tmp_path):
    (tmp_path / "requirements.txt").write_text("celery==5.5.3\n")
    tools = tmp_path / ".claude" / "tools" / "integration_analyzer"
    tools.mkdir(parents=True)
    (tools / "pyproject.toml").write_text("[project]\nname='x'\ndependencies=['httpx']\n")
    (tools / "Dockerfile").write_text("FROM python:3.12-slim-bookworm\n")
    assert [p.name for p in walk_manifests(tmp_path)] == ["requirements.txt"]


def test_requirements_pinned():
    text = "celery==5.5.3\ncybox==2.1.0.21\n# comment\n-r other.txt\nrequests>=2.0\n"
    pkgs = parsers.parse_requirements(text, "requirements.txt")
    names = {p.name: p.version for p in pkgs}
    assert names["celery"] == "5.5.3"
    assert names["cybox"] == "2.1.0.21"
    assert all(p.ecosystem == Ecosystem.pypi for p in pkgs)
    # requests>=2.0 is unpinned with a range operator -> skipped from name-only
    assert "requests" not in names


def test_requirements_extras():
    pkgs = parsers.parse_requirements("uvicorn[standard]==0.30.0\n", "r.txt")
    assert pkgs[0].name == "uvicorn"
    assert pkgs[0].version == "0.30.0"


def test_package_json():
    text = '{"dependencies": {"react": "^18.2.0", "left-pad": "1.3.0"}, "devDependencies": {"vite": "~5.0.0"}}'
    pkgs = parsers.parse_package_json(text, "package.json")
    d = {p.name: p.version for p in pkgs}
    assert d["react"] == "18.2.0"
    assert d["left-pad"] == "1.3.0"
    assert d["vite"] == "5.0.0"
    assert all(p.ecosystem == Ecosystem.npm for p in pkgs)


def test_dockerfile_base_image_and_apt():
    text = (
        "FROM ubuntu:24.04 as build\n"
        "RUN apt-get install -y python3.12 git curl\n"
        "FROM build as final\n"       # references prior stage -> not a base image
        "FROM scratch\n"              # scratch -> skipped
    )
    pkgs = parsers.parse_dockerfile(text, "Dockerfile")
    images = [p for p in pkgs if p.ecosystem == Ecosystem.docker_image]
    apts = {p.name for p in pkgs if p.ecosystem == Ecosystem.apt}
    assert len(images) == 1
    assert images[0].name == "ubuntu" and images[0].version == "24.04"
    assert {"python3.12", "git", "curl"} <= apts


def test_dockerfile_ignores_comment_words_in_continued_install():
    # a `#` comment inside a `\`-continued apt-get install block must not be
    # parsed as package names (regression: "the", "tzdata", prose words)
    text = (
        "RUN set -eux; \\\n"
        "    apt-get install --yes --no-install-recommends \\\n"
        "        ca-certificates \\\n"
        "# Bring in tzdata so users could set the timezones through the environment\n"
        "        tzdata; \\\n"
        "    rm -rf /var/lib/apt/lists/*\n"
    )
    apts = {p.name for p in parsers.parse_dockerfile(text, "Dockerfile") if p.ecosystem == Ecosystem.apt}
    assert apts == {"ca-certificates", "tzdata"}
    for junk in ("the", "Bring", "so", "users", "timezones", "environment"):
        assert junk not in apts


def test_dockerfile_apt_flags_before_install_and_pinned_version():
    # regression: `apt-get -y install` (flag BETWEEN apt-get and install) was
    # skipped entirely, losing nginx; and `pkg=version` pins were discarded
    text = "RUN apt-get update; apt-get -y install nginx=1.28.0-1~jammy gettext-base openssl\n"
    apts = {p.name: p.version for p in parsers.parse_dockerfile(text, "Dockerfile")
            if p.ecosystem == Ecosystem.apt}
    assert apts["nginx"] == "1.28.0-1~jammy"
    assert "gettext-base" in apts and "openssl" in apts


def test_dockerfile_resolves_arg_env_in_apt_install():
    """A real-world mongodb Dockerfile pins MongoDB through variables only. Left
    unresolved, `${MONGO_PACKAGE}` fails the package-name check and the pin is
    lost entirely — the image's MongoDB version appears nowhere in the scan."""
    text = (
        "FROM ubuntu:24.04\n"
        "ARG MONGO_PACKAGE=mongodb-org\n"
        "ENV MONGO_PACKAGE=${MONGO_PACKAGE}\n"          # ENV re-exports the ARG default
        "ENV MONGO_VERSION 8.3.4\n"                      # legacy space-separated form
        "RUN apt-get update \\\n"
        "  && apt-get install -y \\\n"
        "    ${MONGO_PACKAGE}=$MONGO_VERSION \\\n"
        "    ${MONGO_PACKAGE}-server=$MONGO_VERSION\n"
    )
    apts = {p.name: p.version for p in parsers.parse_dockerfile(text, "Dockerfile")
            if p.ecosystem == Ecosystem.apt}
    assert apts == {"mongodb-org": "8.3.4", "mongodb-org-server": "8.3.4"}


def test_dockerfile_version_vars_become_components():
    """openssl/erlang/rabbitmq are built from source in a typical rabbitmq image —
    no manifest declares them, so a `<PRODUCT>_VERSION` ARG/ENV is the only
    record of what version ships."""
    text = (
        "FROM ubuntu:24.04\n"
        "ENV OPENSSL_VERSION=3.5.7\n"
        "ENV OTP_VERSION=27.3.4.13\n"
        "ENV RABBITMQ_VERSION=4.3.2\n"
        "ARG BUILD_VERSION=2.1.0\n"     # names the image itself, not a product
        "ENV NODE_VERSION=20\n"         # major-only: not a resolvable version pin
        "ENV MONGO_MAJOR 8.3\n"         # not a _VERSION var
    )
    got = {p.name: p.version for p in parsers.parse_dockerfile(text, "Dockerfile")
           if p.ecosystem == Ecosystem.other}
    assert got == {"openssl": "3.5.7", "otp": "27.3.4.13", "rabbitmq": "4.3.2"}


def test_dockerfile_unresolvable_var_does_not_invent_a_package():
    text = "FROM ubuntu:24.04\nRUN apt-get install -y ${NOT_DECLARED} curl\n"
    apts = {p.name for p in parsers.parse_dockerfile(text, "Dockerfile")
            if p.ecosystem == Ecosystem.apt}
    assert apts == {"curl"}


def test_dockerfile_registry_and_digest():
    pkgs = parsers.parse_dockerfile("FROM docker.io/library/node:18@sha256:abc\n", "Dockerfile")
    img = [p for p in pkgs if p.ecosystem == Ecosystem.docker_image][0]
    assert img.name == "node"
    assert img.version == "18"


def test_package_lock_v3():
    text = '''{"lockfileVersion":3,"packages":{
      "": {"name":"root"},
      "node_modules/react": {"version":"18.2.0"},
      "node_modules/scheduler": {"version":"0.23.0"}
    }}'''
    pkgs = parsers.parse_package_lock(text, "package-lock.json")
    d = {p.name: p for p in pkgs}
    assert d["react"].version == "18.2.0"
    assert d["scheduler"].is_direct is False


def test_vendored_lib_dist_info_and_source(tmp_path):
    # a plugin that vendors deps under lib/ with no requirements.txt
    lib = tmp_path / "my_plugin" / "lib"
    # dist-info (versioned, authoritative via METADATA)
    di = lib / "sqlalchemy-2.0.43.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text("Name: SQLAlchemy\nVersion: 2.0.43\n")
    # source-only package with __version__
    illumio = lib / "illumio"
    illumio.mkdir()
    (illumio / "__init__.py").write_text('__version__ = "1.1.3"\n')
    # source-only package without version
    boto = lib / "botocore"
    boto.mkdir()
    (boto / "__init__.py").write_text("# no version here\n")
    # junk that must be ignored
    (lib / "__pycache__").mkdir()
    (lib / "__init__.py").write_text("")

    pkgs = extract_vendored(tmp_path)
    got = {p.name: p.version for p in pkgs}
    assert got["SQLAlchemy"] == "2.0.43"      # from METADATA
    assert got["illumio"] == "1.1.3"          # from __version__
    assert got["botocore"] is None            # source-only, no version
    assert "__pycache__" not in got and "__init__.py" not in got
    assert all(p.ecosystem == Ecosystem.pypi for p in pkgs)


def test_dist_info_rejects_bogus_version(tmp_path):
    di = tmp_path / "lib" / "invoke-2.2.0.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text("Name: invoke\nVersion: .\n")  # bogus version
    pkg = parsers.parse_dist_info(di, "lib")
    assert pkg.name == "invoke"
    assert pkg.version == "2.2.0"   # falls back to the dir name, not "."


def test_pyproject_pep621():
    text = '[project]\nname="x"\ndependencies=["httpx==0.27.0","fastapi>=0.100"]\n'
    pkgs = parsers.parse_pyproject(text, "pyproject.toml")
    d = {p.name: p.version for p in pkgs}
    assert d["httpx"] == "0.27.0"
    assert d["fastapi"] is None


def test_parse_compose_images_as_docker_packages():
    text = (
        "version: '3'\n"
        "services:\n"
        "  core:\n"
        "    image: acme/platform:core-6.1.0\n"
        "  ui:\n"
        "    image: docker.io/library/nginx:1.25\n"     # registry + library/ stripped
        "  db:\n"
        "    image: ${MONGO_IMAGE}\n"                    # env interpolation → skipped
    )
    pkgs = parsers.parse_compose(text, "docker-compose.yml")
    got = {p.name: p.version for p in pkgs}
    # Docker Hub namespace kept (no registry host); docker.io/library/ stripped.
    assert got == {"acme/platform": "core-6.1.0", "nginx": "1.25"}
    assert all(p.ecosystem == Ecosystem.docker_image for p in pkgs)
    assert all(p.is_direct for p in pkgs)


def test_parse_compose_skips_images_named_by_a_supplied_tag():
    """A deployment compose repo may name none of its images: every service takes the
    whole reference from a variable the user supplies out-of-band. Those are the
    project's own build artifacts — no upstream release stream, and all four
    collapse to one meaningless component name — so they are never components.
    They stay image-inspection targets: their dpkg inventory is real data."""
    text = (
        "services:\n"
        "  rabbitmq:\n"
        "    image: index.docker.io/${RABBITMQ_TAG}\n"
        "  core:\n"
        "    image: index.docker.io/${CORE_TAG}\n"
        "  ui:\n"
        "    image: index.docker.io/${UI_TAG}\n"          # no value → skipped everywhere
    )
    tag_vars = {
        "RABBITMQ_TAG": "acme/platform:rabbitmq-6.1.0-4.2.2",
        "CORE_TAG": "acme/platform-dev:core-latest",
    }
    assert parsers.parse_compose(text, "docker-compose.yml", tag_vars) == []
    assert parsers.compose_image_refs(text, tag_vars) == [
        "index.docker.io/acme/platform:rabbitmq-6.1.0-4.2.2",
        "index.docker.io/acme/platform-dev:core-latest",
    ]


def test_parse_compose_keeps_image_whose_variable_supplies_only_the_tag():
    """`mongo:${MONGO_TAG}` still names the product — the repo says *what* the
    image is, only its version comes from outside. That is a genuine upstream
    component, unlike a reference whose name itself is supplied."""
    text = (
        "services:\n"
        "  db:\n"
        "    image: mongo:${MONGO_TAG}\n"
        "  cache:\n"
        "    image: ${REGISTRY}/redis:7.2\n"      # var supplies only the registry
    )
    tag_vars = {"MONGO_TAG": "7.0.14", "REGISTRY": "mirror.internal.io"}
    got = {p.name: p.version for p in parsers.parse_compose(text, "docker-compose.yml", tag_vars)}
    assert got == {"mongo": "7.0.14", "redis": "7.2"}


def test_parse_compose_skips_locally_built_services():
    """A service with a `build:` key names an image Docker builds on the spot —
    the tag exists in no registry (e.g. a dev-tooling
    `docker-compose.yml` that builds a local helper image). It has no
    upstream release stream to track, so it must never become a package."""
    text = (
        "services:\n"
        "  analyzer:\n"
        "    build: .\n"
        "    image: integration-analyzer:latest\n"
        "  builder:\n"
        "    build:\n"
        "      context: ./svc\n"
        "      dockerfile: Dockerfile\n"
        "    image: ${LOCAL_TAG}\n"           # var only used by a built service
        "  redis:\n"
        "    image: redis:7\n"                # genuinely pulled → kept
    )
    assert [p.name for p in parsers.parse_compose(text, "docker-compose.yml")] == ["redis"]
    assert parsers.compose_image_refs(text) == ["redis:7"]
    # and we never prompt for a tag variable only a locally-built service uses
    assert parsers.compose_tag_vars(text) == []


def test_resolve_image_ref_defaults_and_forms():
    tv = {"CORE_TAG": "acme/app:core-1.0"}
    assert parsers.resolve_image_ref("index.docker.io/${CORE_TAG}", tv) == "index.docker.io/acme/app:core-1.0"
    assert parsers.resolve_image_ref("$CORE_TAG", tv) == "acme/app:core-1.0"
    # supplied value wins over the compose default, default used when unsupplied
    assert parsers.resolve_image_ref("${CORE_TAG:-acme/app:fallback}", tv) == "acme/app:core-1.0"
    assert parsers.resolve_image_ref("${OTHER_TAG:-acme/app:fallback}", tv) == "acme/app:fallback"
    # no value anywhere → unresolved
    assert parsers.resolve_image_ref("index.docker.io/${UI_TAG}", tv) is None
    assert parsers.resolve_image_ref("${UI_TAG}", {"UI_TAG": "  "}) is None


def test_compose_tag_vars_lists_variables_needing_values():
    text = (
        "services:\n"
        "  a:\n"
        "    image: index.docker.io/${CORE_TAG}\n"
        "  b:\n"
        "    image: index.docker.io/${MONGO_TAG}\n"
        "  c:\n"
        "    image: redis:7\n"
        "  d:\n"
        "    image: index.docker.io/${CORE_TAG}\n"       # deduped
    )
    assert parsers.compose_tag_vars(text) == ["CORE_TAG", "MONGO_TAG"]


def test_parse_file_routes_compose(tmp_path):
    f = tmp_path / "docker-compose.prod.yml"
    f.write_text("services:\n  x:\n    image: redis:7\n")
    pkgs = parsers.parse_file(f, "docker-compose.prod.yml")
    assert pkgs[0].name == "redis" and pkgs[0].version == "7"
    assert pkgs[0].ecosystem == Ecosystem.docker_image
