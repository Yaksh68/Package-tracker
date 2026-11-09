"""Deterministic shallow-clone step (runs before the extraction agent).

The access token is picked by URL host and injected into the clone URL at
runtime — it is never written to snapshots or logs. Any token that leaks into
git's stderr is redacted before the error is surfaced.

The URL and branch are user input. They pass `git_rules` checks first, then
git gets the URL after `--` (so it can never be read as an option). Git may
only use http(s) (GIT_ALLOW_PROTOCOL), never prompts for credentials, and is
killed after `clone_timeout_seconds`. In the public mode the host must
also resolve to a public address, and redirects are refused.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunparse

from .. import scan_context
from ..config import get_settings
from ..git_rules import RepoUrlError, check_branch, check_repo_url, require_public_address


class CloneError(RuntimeError):
    pass


@dataclass
class Clone:
    path: Path
    _tmp: str

    def cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> "Clone":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


def _token_for(url: str, override: str | None) -> str | None:
    """The repo's own token, else its workspace's git token for that host
    (the env's in the local mode); see scan_context."""
    if override:
        return override
    return scan_context.git_token((urlparse(url).hostname or "").lower())


def _auth_url(url: str, token: str | None) -> str:
    if not token:
        return url
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    # GitHub accepts the bare token as userinfo; GitLab expects oauth2:<token>.
    userinfo = token if "github" in host else f"oauth2:{token}"
    netloc = f"{userinfo}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunparse(parts._replace(netloc=netloc))


def _redact(text: str, *secrets: str | None) -> str:
    out = text
    for sec in secrets:
        if sec:
            out = out.replace(sec, "***")
    # also scrub any userinfo that slipped through
    out = re.sub(r"://[^@/\s]+@", "://***@", out)
    return out


@dataclass
class RemoteInfo:
    default_branch: str | None
    branches: set[str]


_LS_REMOTE_TIMEOUT = 30


def remote_info(url: str, token: str | None = None) -> RemoteInfo:
    """Read a repo's branches and default branch with `git ls-remote`, without
    cloning (a free scan checks the repo before using up the visitor's scan).
    The same rules as a clone; CloneError when the repo can't be read."""
    public = get_settings().public_mode()
    try:
        url = check_repo_url(url)
        if public:
            require_public_address((urlsplit(url).hostname or "").lower())
    except RepoUrlError as e:
        raise CloneError(f"refused: {e}") from e
    auth_url = _auth_url(url, token)
    cmd = ["git"] + (["-c", "http.followRedirects=false"] if public else [])
    cmd += ["ls-remote", "--symref", "--", auth_url, "HEAD", "refs/heads/*"]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https" if public else "http:https"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=_LS_REMOTE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise CloneError(f"{url} didn't answer within {_LS_REMOTE_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise CloneError(_redact(proc.stderr.strip() or "git ls-remote failed", token, auth_url))
    default, branches = None, set()
    for line in proc.stdout.splitlines():
        head, _, ref = line.partition("\t")
        if head.startswith("ref: refs/heads/") and ref == "HEAD":
            default = head[len("ref: refs/heads/"):]
        elif ref.startswith("refs/heads/"):
            branches.add(ref[len("refs/heads/"):])
    return RemoteInfo(default_branch=default, branches=branches)


def shallow_clone(url: str, branch: str, token_override: str | None = None) -> Clone:
    """`git clone --depth 1 --branch <branch>` into a temp dir. Caller cleans up
    (use as a context manager)."""
    settings = get_settings()
    public = settings.public_mode()
    try:
        url = check_repo_url(url)
        branch = check_branch(branch)
        if public:
            require_public_address((urlsplit(url).hostname or "").lower())
    except RepoUrlError as e:
        raise CloneError(f"clone refused: {e}") from e

    token = _token_for(url, token_override)
    auth_url = _auth_url(url, token)
    tmp = tempfile.mkdtemp(prefix="packages-tracker-clone-")
    dest = Path(tmp) / "repo"
    cmd = ["git"]
    if public:
        cmd += ["-c", "http.followRedirects=false"]
    cmd += [
        "clone", "--depth", "1", "--single-branch",
        "--branch", branch, "--", auth_url, str(dest),
    ]
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",  # fail instead of waiting for a password
        "GIT_ALLOW_PROTOCOL": "https" if public else "http:https",
    }
    timeout = settings.clone_timeout_seconds
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise CloneError(f"clone {url}@{branch} timed out after {timeout}s") from None
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        msg = _redact(proc.stderr.strip() or "git clone failed", token, auth_url)
        raise CloneError(f"clone {url}@{branch} failed: {msg}")
    return Clone(path=dest, _tmp=tmp)
