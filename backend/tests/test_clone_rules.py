"""Repo URLs and branch names reach `git clone`. Anything that git could read
as an option, as a local path, or through a non-http(s) transport is refused
before git runs (phase 0, task 0.2)."""
import socket
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import git_rules, scheduler
from app.agents import clone
from app.agents.clone import CloneError, shallow_clone
from app.config import get_settings
from app.git_rules import RepoUrlError, check_branch, check_repo_url
from app.main import app


def _mode(monkeypatch, auth_mode="none", allowlist="", **env):
    monkeypatch.setenv("AUTH_MODE", auth_mode)
    monkeypatch.setenv("GIT_HOST_ALLOWLIST", allowlist)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def _resolves_to(monkeypatch, *ips):
    def fake(host, port):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, 0)) for ip in ips]
    monkeypatch.setattr(git_rules.socket, "getaddrinfo", fake)


# --------------------------------------------------------------------------- #
# URL rules                                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "--upload-pack=touch /tmp/pwned",
    "-uhttps://github.com/x/y",
    "file:///etc/passwd",
    "/etc/passwd",
    "C:\\Windows\\System32",
    "ssh://git@github.com/x/y.git",
    "git@github.com:x/y.git",
    "ext::sh -c touch% /tmp/pwned",
    "https://user:token@github.com/x/y.git",
    "https://token@github.com/x/y.git",
    "https://github.com/x/y.git\n--upload-pack=x",
    "https://github.com:notaport/x/y.git",
    "https://",
    "",
])
@pytest.mark.parametrize("mode", ["none", "github"])
def test_dangerous_urls_are_rejected_in_every_mode(monkeypatch, mode, url):
    _mode(monkeypatch, mode)
    with pytest.raises(RepoUrlError):
        check_repo_url(url)


def test_plain_http_is_only_allowed_in_the_local_mode(monkeypatch):
    _mode(monkeypatch, "none")
    assert check_repo_url(" http://git.corp.internal/x.git ") == "http://git.corp.internal/x.git"
    _mode(monkeypatch, "github")
    with pytest.raises(RepoUrlError, match="https://"):
        check_repo_url("http://github.com/x/y.git")


def test_public_mode_uses_the_default_allowlist_and_refuses_ip_hosts(monkeypatch):
    _mode(monkeypatch, "github")
    assert check_repo_url("https://github.com/x/y.git")
    assert check_repo_url("https://gitlab.com/group/project")
    with pytest.raises(RepoUrlError, match="isn't allowed"):
        check_repo_url("https://evil.example/x.git")
    with pytest.raises(RepoUrlError, match="IP address"):
        check_repo_url("https://140.82.112.3/x/y.git")


def test_local_mode_accepts_any_host_until_an_allowlist_is_set(monkeypatch):
    _mode(monkeypatch, "none")
    assert check_repo_url("https://gitlab.corp.internal/team/app.git")
    assert check_repo_url("https://10.1.2.3/team/app.git")  # internal servers by IP are fine locally

    _mode(monkeypatch, "none", "gitlab.corp.internal, *.example.com")
    assert check_repo_url("https://gitlab.corp.internal/team/app.git")
    assert check_repo_url("https://git.example.com/x.git")
    with pytest.raises(RepoUrlError):
        check_repo_url("https://example.com/x.git")  # "*." means subdomains only
    with pytest.raises(RepoUrlError):
        check_repo_url("https://github.com/x/y.git")


# --------------------------------------------------------------------------- #
# Branch rules                                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("branch", ["main", "release/6.1", "develop-7.0.0", "feature/x_y", "v1.2.3"])
def test_valid_branches_pass(branch):
    assert check_branch(f"  {branch} ") == branch


@pytest.mark.parametrize("branch", [
    "-x", "--upload-pack=x", "a..b", "a b", "a\tb", "a~1", "a^", "a:b", "a?", "a*", "a[",
    "a\\b", "/a", "a/", "a.", "a//b", "a@{1}", "@", ".hidden", "a/.b", "x.lock", "a/b.lock", "",
])
def test_invalid_branches_are_rejected(branch):
    with pytest.raises(RepoUrlError):
        check_branch(branch)


# --------------------------------------------------------------------------- #
# The git invocation                                                           #
# --------------------------------------------------------------------------- #
class _Ran:
    cmd = None
    env = None
    timeout = None


@pytest.fixture
def fake_git(monkeypatch):
    ran = _Ran()

    def run(cmd, capture_output, text, env, timeout):
        ran.cmd, ran.env, ran.timeout = cmd, env, timeout
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(clone.subprocess, "run", run)
    return ran


def test_the_url_goes_after_double_dash_and_git_cannot_prompt(monkeypatch, fake_git):
    _mode(monkeypatch, "none")
    shallow_clone("https://git.corp.internal/team/app.git", "release/6.1").cleanup()
    cmd = fake_git.cmd
    assert cmd[cmd.index("--") + 1] == "https://git.corp.internal/team/app.git"
    assert cmd[cmd.index("--branch") + 1] == "release/6.1"
    assert fake_git.env["GIT_TERMINAL_PROMPT"] == "0"
    assert fake_git.env["GIT_ALLOW_PROTOCOL"] == "http:https"
    assert fake_git.timeout == 120
    assert "http.followRedirects=false" not in cmd


def test_public_mode_refuses_redirects_and_non_https_transports(monkeypatch, fake_git):
    _mode(monkeypatch, "github")
    _resolves_to(monkeypatch, "140.82.112.3")
    shallow_clone("https://github.com/x/y.git", "main").cleanup()
    assert fake_git.cmd[:3] == ["git", "-c", "http.followRedirects=false"]
    assert fake_git.env["GIT_ALLOW_PROTOCOL"] == "https"


@pytest.mark.parametrize("ip", ["10.0.0.7", "127.0.0.1", "169.254.169.254", "100.64.1.1",
                                "192.168.1.5", "::1", "::ffff:192.168.1.5", "fe80::1%eth0"])
def test_public_mode_refuses_hosts_that_resolve_to_internal_addresses(monkeypatch, fake_git, ip):
    _mode(monkeypatch, "github")
    _resolves_to(monkeypatch, "140.82.112.3", ip)  # one bad address is enough
    with pytest.raises(CloneError, match="non-public address"):
        shallow_clone("https://github.com/x/y.git", "main")
    assert fake_git.cmd is None  # git never ran


def test_local_mode_does_not_resolve_hosts(monkeypatch, fake_git):
    _mode(monkeypatch, "none")

    def no_dns(*a, **kw):
        raise AssertionError("the local mode must not do the public-address check")

    monkeypatch.setattr(git_rules.socket, "getaddrinfo", no_dns)
    shallow_clone("https://10.1.2.3/team/app.git", "main").cleanup()
    assert fake_git.cmd is not None


def test_option_shaped_urls_are_refused_even_if_already_stored(monkeypatch, fake_git):
    _mode(monkeypatch, "none")
    with pytest.raises(CloneError, match="clone refused"):
        shallow_clone("--upload-pack=touch /tmp/x", "main")
    with pytest.raises(CloneError, match="clone refused"):
        shallow_clone("https://git.corp.internal/app.git", "--upload-pack=x")
    assert fake_git.cmd is None


def test_a_hung_clone_is_killed_and_reported(monkeypatch):
    _mode(monkeypatch, "none", CLONE_TIMEOUT_SECONDS="5")

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(clone.subprocess, "run", run)
    with pytest.raises(CloneError, match="timed out after 5s"):
        shallow_clone("https://git.corp.internal/app.git", "main")


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch, **kw: None)
    return TestClient(app)


def test_api_rejects_dangerous_repo_urls_and_branches(client):
    r = client.post("/api/repos", json={"url": "--upload-pack=touch /tmp/x", "branches": ["main"]})
    assert r.status_code == 422
    r = client.post("/api/repos", json={"url": "file:///etc/passwd", "branches": ["main"]})
    assert r.status_code == 422
    r = client.post("/api/repos", json={"url": "https://x/ok", "branches": ["--upload-pack=x"]})
    assert r.status_code == 422


def test_api_checks_branches_on_edit_for_git_repos_only(client):
    rid = client.post("/api/repos", json={"url": "https://x/edit-me", "branches": ["main"]}).json()["id"]
    r = client.put(f"/api/repos/{rid}", json={"branches": ["main", "-x"]})
    assert r.status_code == 422
    r = client.put(f"/api/repos/{rid}", json={"branches": ["main", "release/7.0"]})
    assert r.status_code == 200
    assert r.json()["branches"] == ["main", "release/7.0"]
