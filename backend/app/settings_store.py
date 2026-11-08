"""Runtime, UI-editable settings, per workspace (`workspace_settings`).

They override the env defaults from config.py at runtime, so a toggle like
"AI research on/off" takes effect from the next scan without a restart. Only
an allowlist of keys is editable. API keys and tokens aren't settings: they
live in `secrets` (accounts.set_secret).

Defaults:
- The local workspace in the local mode uses the env, as before workspaces.
- Any other workspace starts with AI research off and no provider, until
  someone in it configures one (users bring their own keys).

Without an explicit workspace, the current scan's workspace is used, or the
local workspace outside a scan.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from . import accounts, scan_context
from .ai.config import PRESET_BASE_URLS
from .config import get_settings
from .git_rules import RepoUrlError, public_addresses
from .models import LOCAL_WORKSPACE_ID

_BOOL_KEYS = {"use_enrichment_agent", "use_extraction_agent"}
_TEXT_KEYS = {"ai_provider", "ai_model", "ai_base_url", "search_provider"}
_EDITABLE = _BOOL_KEYS | _TEXT_KEYS
AI_PROVIDERS = ("", "claude-cli", "anthropic", *PRESET_BASE_URLS, "openai-compatible")
SEARCH_PROVIDERS = ("", "tavily", "exa")


def _workspace(workspace_id: str | None) -> str:
    return workspace_id or scan_context.workspace_id() or LOCAL_WORKSPACE_ID


def defaults(workspace_id: str) -> dict:
    env = get_settings()
    if workspace_id == LOCAL_WORKSPACE_ID and not env.public_mode():
        return {
            "use_enrichment_agent": env.use_enrichment_agent,
            "use_extraction_agent": env.use_extraction_agent,
            "ai_provider": env.ai_provider, "ai_model": env.ai_model,
            "ai_base_url": env.ai_base_url, "search_provider": env.search_provider,
        }
    return {"use_enrichment_agent": False, "use_extraction_agent": False,
            "ai_provider": "", "ai_model": "", "ai_base_url": "", "search_provider": ""}


def get_runtime_settings(workspace_id: str | None = None) -> dict:
    ws = _workspace(workspace_id)
    base = defaults(ws)
    base.update({k: v for k, v in accounts.workspace_settings(ws).items() if k in _EDITABLE})
    return base


def _clean(patch: dict) -> dict:
    out = {}
    for k, v in patch.items():
        if v is None or k not in _EDITABLE:
            continue
        out[k] = bool(v) if k in _BOOL_KEYS else str(v).strip()
    provider = out.get("ai_provider")
    if provider is not None:
        out["ai_provider"] = provider = provider.lower()
        if provider not in AI_PROVIDERS:
            raise ValueError(f"unknown AI provider {provider!r}")
        if provider == "claude-cli" and get_settings().public_mode():
            raise ValueError("the Claude CLI can't be used in the public mode")
    search = out.get("search_provider")
    if search is not None:
        out["search_provider"] = search = search.lower()
        if search not in SEARCH_PROVIDERS:
            raise ValueError(f"unknown search provider {search!r}")
    if out.get("ai_base_url"):
        check_ai_base_url(out["ai_base_url"])
    return out


def check_ai_base_url(url: str) -> None:
    """Raise ValueError for a base URL the server shouldn't call. The local
    mode takes any http(s) URL (a local Ollama, say). In the public mode it's
    a URL a user typed, which the server then calls, so it must be https on a
    hostname that resolves to public addresses only. The scan checks the
    address again on every request (safe_http)."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 (raises ValueError for a bad port)
    except ValueError as e:
        raise ValueError(f"that isn't a valid URL: {e}") from e
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("the AI base URL must start with https:// or http://")
    if not get_settings().public_mode():
        return
    if parts.scheme != "https":
        raise ValueError("the AI base URL must start with https://")
    if parts.username or parts.password:
        raise ValueError("put the key in the API key field, not in the URL")
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("use the provider's hostname, not an IP address")
    try:
        public_addresses(parts.hostname)
    except RepoUrlError as e:
        raise ValueError(str(e)) from e


def update_runtime_settings(patch: dict, workspace_id: str | None = None) -> dict:
    """Apply the editable keys of `patch`; raises ValueError for a bad value."""
    ws = _workspace(workspace_id)
    accounts.set_workspace_settings(ws, _clean(patch))
    return get_runtime_settings(ws)
