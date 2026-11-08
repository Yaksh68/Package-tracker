"""Which AI provider a scan uses, and with what credentials.

Each workspace chooses its own provider: the provider, model, base URL and
search provider are workspace settings (settings_store), and the AI and
search keys are encrypted workspace secrets (accounts). Inside a scan, the
scan's workspace applies (scan_context); outside one, the local workspace.
The env (AI_PROVIDER, AI_MODEL, AI_API_KEY, AI_BASE_URL, SEARCH_PROVIDER,
SEARCH_API_KEY) only supplies defaults for the local workspace in the local
mode. The public mode never uses the server's own keys.

    AI_PROVIDER unset   → the Claude CLI and its login (local mode only; it's
                          what the app did before providers existed)
    claude-cli          → the same, explicitly
    anthropic           → the Anthropic API with AI_API_KEY (or ANTHROPIC_API_KEY)
    openai, gemini, groq, openrouter
                        → that provider's OpenAI-compatible API with AI_API_KEY,
                          searching through SEARCH_PROVIDER (tavily / exa)
    openai-compatible   → any other OpenAI-compatible API at AI_BASE_URL
                          (Mistral, GitHub Models, a local Ollama…)

`resolve()` never raises: it returns (config, None) or (None, why-not), and
the enrichment agent reports the why-not as ai_status "no_key".
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import Settings, get_settings

# OpenAI-compatible endpoints, checked against each provider's docs (2026-11).
PRESET_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
SEARCH_PROVIDERS = ("tavily", "exa")
DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"


@dataclass(frozen=True)
class AiConfig:
    provider: str              # claude-cli | anthropic | openai-compatible
    model: str
    preset: str | None = None  # openai / gemini / groq / openrouter / None (custom)
    api_key: str | None = None
    base_url: str | None = None
    search_provider: str | None = None
    search_api_key: str | None = None
    max_tool_rounds: int = 8

    @property
    def label(self) -> str:
        """What scan logs and scan_runs record, e.g. "anthropic" or
        "openai-compatible:groq". Never includes a key."""
        if self.provider == "openai-compatible":
            return f"openai-compatible:{self.preset or 'custom'}"
        return self.provider


def resolve(settings: Settings | None = None, *,
            workspace_id: str | None = None) -> tuple[AiConfig | None, str | None]:
    from .. import accounts, scan_context, settings_store  # (they import this module)
    from ..models import LOCAL_WORKSPACE_ID

    s = settings or get_settings()
    ws = workspace_id or scan_context.workspace_id() or LOCAL_WORKSPACE_ID
    rt = settings_store.get_runtime_settings(ws)
    # the server's own keys back only the local workspace in the local mode
    env_ok = ws == LOCAL_WORKSPACE_ID and not s.public_mode()

    def secret(name: str, env_value: str | None) -> str | None:
        value = accounts.get_secret(ws, name) or (env_value if env_ok else None)
        return (value or "").strip() or None

    provider = (rt.get("ai_provider") or "").strip().lower()
    model = (rt.get("ai_model") or "").strip()
    key = secret("ai_api_key", s.ai_api_key)

    if provider in ("", "claude-cli"):
        if s.public_mode():
            return None, ("no AI provider is configured" if not provider else
                          "the Claude CLI can't be used in the public mode")
        return AiConfig(provider="claude-cli", model=model or s.agent_model), None

    if provider == "anthropic":
        if not key and env_ok:
            key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None
        if not key:
            return None, "add an Anthropic API key"
        return AiConfig(provider="anthropic", model=model or DEFAULT_CLAUDE_MODEL, api_key=key), None

    if provider in PRESET_BASE_URLS or provider == "openai-compatible":
        preset = provider if provider in PRESET_BASE_URLS else None
        base_url = PRESET_BASE_URLS.get(provider) or (rt.get("ai_base_url") or "").strip()
        if not base_url:
            return None, "set the base URL for an openai-compatible provider"
        if preset and not key:
            return None, f"add a {provider} API key"
        if not model:
            return None, "choose a model: free model names change too often for a default"
        search = (rt.get("search_provider") or "").strip().lower()
        search_key = secret("search_api_key", s.search_api_key)
        if search not in SEARCH_PROVIDERS or not search_key:
            return None, ("this provider has no built-in web search: set SEARCH_PROVIDER "
                          "(tavily or exa) and SEARCH_API_KEY")
        return AiConfig(
            provider="openai-compatible", model=model, preset=preset, api_key=key,
            base_url=base_url, search_provider=search, search_api_key=search_key,
            max_tool_rounds=max(1, s.ai_max_tool_rounds),
        ), None

    return None, f"unknown AI_PROVIDER {provider!r}"
