"""Who a scan runs for: its workspace, and the user who added the repo.

The pipeline binds a ScanContext at the start of every scan, the way scanlog
binds its logger, so code deep inside a scan can find that workspace's
settings, AI provider, git tokens and GitHub API token without passing them
through every function. Outside a scan (the API, the eval runner) there's no
context, and callers say which workspace they mean.

Where credentials come from (first match wins):
  git tokens        workspace secret (github_token / gitlab_token) → the env
                    (local mode only)
  GitHub API token  workspace secret github_token → the repo creator's GitHub
                    sign-in token → the env GITHUB_API_TOKEN / GITHUB_TOKEN
                    (local mode only) → none
The public mode never falls back to the server's own env tokens: users bring
their own.

A free scan without an account (`trial`) uses only the token the visitor gave
for that scan, held here in memory and gone when the scan ends, for the repo's
own host. Its GitHub lookups fall back to TRIAL_GITHUB_TOKEN, if the server
has one.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field

from .config import get_settings
from .models import LOCAL_WORKSPACE_ID

_MISSING = object()


@dataclass
class ScanContext:
    workspace_id: str = LOCAL_WORKSPACE_ID
    created_by: str | None = None
    # a free scan without an account: the visitor's token (never stored) and
    # the host it's for
    trial: bool = False
    trial_token: str | None = field(default=None, repr=False)
    repo_host: str = ""
    _memo: dict = field(default_factory=dict, repr=False)

    def _cached(self, key: str, compute):
        value = self._memo.get(key, _MISSING)
        if value is _MISSING:
            value = self._memo[key] = compute()
        return value

    def secret(self, name: str) -> str | None:
        from . import accounts
        return self._cached(f"secret:{name}", lambda: accounts.get_secret(self.workspace_id, name))

    def git_token(self, host: str) -> str | None:
        host = (host or "").lower()
        if self.trial:
            return self.trial_token if host and host == self.repo_host else None
        name = "github_token" if "github" in host else "gitlab_token" if "gitlab" in host else None
        if name is None:
            return None
        token = self.secret(name)
        if token:
            return token
        s = get_settings()
        if s.public_mode():
            return None
        return s.github_token if name == "github_token" else s.gitlab_token

    def github_api_token(self) -> str | None:
        def compute():
            if self.trial:
                if self.trial_token and self.repo_host == "github.com":
                    return self.trial_token
                return get_settings().trial_github_token or None
            token = self.secret("github_token")
            if token:
                return token
            if self.created_by:
                from . import accounts
                token = accounts.user_github_token(self.created_by)
                if token:
                    return token
            s = get_settings()
            return None if s.public_mode() else s.github_read_token()
        return self._cached("github_api_token", compute)

    def reject_github_api_token(self) -> bool:
        """GitHub refused the token (revoked, expired): the rest of this scan
        goes without one. True the first time, so the caller can say so once."""
        if self._memo.get("github_api_token", _MISSING) is None:
            return False
        self._memo["github_api_token"] = None
        return True


_current: contextvars.ContextVar[ScanContext | None] = contextvars.ContextVar("scan_context", default=None)


def bind(ctx: ScanContext) -> None:
    _current.set(ctx)


def current() -> ScanContext | None:
    return _current.get()


def workspace_id() -> str | None:
    ctx = _current.get()
    return ctx.workspace_id if ctx else None


def git_token(host: str) -> str | None:
    """The token for cloning from `host`, in the current scan's workspace (or
    the local mode's env outside a scan)."""
    ctx = _current.get() or ScanContext()
    return ctx.git_token(host)


def github_api_token() -> str | None:
    ctx = _current.get() or ScanContext()
    return ctx.github_api_token()


def reject_github_api_token() -> bool:
    """See ScanContext.reject_github_api_token. Outside a scan there's nothing
    to remember it in, so it's False."""
    ctx = _current.get()
    return ctx.reject_github_api_token() if ctx else False
