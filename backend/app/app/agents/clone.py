"""Deterministic shallow-clone step (runs before the extraction agent).

The access token is picked by URL host and injected into the clone URL at
runtime — it is never written to snapshots or logs. Any token that leaks into
git's stderr is redacted before the error is surfaced.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from ..config import get_settings


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
    if override:
        return override
    host = (urlparse(url).hostname or "").lower()
    s = get_settings()
    if "github" in host:
        return s.github_token
    if "gitlab" in host:
        return s.gitlab_token
    return None


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


def shallow_clone(url: str, branch: str, token_override: str | None = None) -> Clone:
    """`git clone --depth 1 --branch <branch>` into a temp dir. Caller cleans up
    (use as a context manager)."""
    token = _token_for(url, token_override)
    auth_url = _auth_url(url, token)
    tmp = tempfile.mkdtemp(prefix="packages-tracker-clone-")
    dest = Path(tmp) / "repo"
    cmd = [
        "git", "clone", "--depth", "1", "--single-branch",
        "--branch", branch, auth_url, str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        msg = _redact(proc.stderr.strip() or "git clone failed", token, auth_url)
        raise CloneError(f"clone {url}@{branch} failed: {msg}")
    return Clone(path=dest, _tmp=tmp)
