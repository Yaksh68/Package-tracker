"""Which AI provider a scan uses, and with what credentials.

Phase 1 reads it from the environment (AI_PROVIDER, AI_MODEL, AI_API_KEY,
AI_BASE_URL, SEARCH_PROVIDER, SEARCH_API_KEY); Phase 2a moves it to each
workspace's settings and encrypted secrets, with the env as the local-mode
fallback.

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


def resolve(settings: Settings | None = None) -> tuple[AiConfig | None, str | None]:
    s = settings or get_settings()
    provider = (s.ai_provider or "").strip().lower()
    model = (s.ai_model or "").strip()
    key = (s.ai_api_key or "").strip() or None

    if provider in ("", "claude-cli"):
        if s.public_mode():
            return None, ("no AI provider is configured" if not provider else
                          "the Claude CLI can't be used in the public mode")
        return AiConfig(provider="claude-cli", model=model or s.agent_model), None

    if provider == "anthropic":
        key = key or (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None
        if not key:
            return None, "add an Anthropic API key (AI_API_KEY)"
        return AiConfig(provider="anthropic", model=model or DEFAULT_CLAUDE_MODEL, api_key=key), None

    if provider in PRESET_BASE_URLS or provider == "openai-compatible":
        preset = provider if provider in PRESET_BASE_URLS else None
        base_url = PRESET_BASE_URLS.get(provider) or (s.ai_base_url or "").strip()
        if not base_url:
            return None, "set AI_BASE_URL for an openai-compatible provider"
        if preset and not key:
            return None, f"add a {provider} API key (AI_API_KEY)"
        if not model:
            return None, "set AI_MODEL: free model names change too often for a default"
        search = (s.search_provider or "").strip().lower()
        search_key = (s.search_api_key or "").strip() or None
        if search not in SEARCH_PROVIDERS or not search_key:
            return None, ("this provider has no built-in web search: set SEARCH_PROVIDER "
                          "(tavily or exa) and SEARCH_API_KEY")
        return AiConfig(
            provider="openai-compatible", model=model, preset=preset, api_key=key,
            base_url=base_url, search_provider=search, search_api_key=search_key,
            max_tool_rounds=max(1, s.ai_max_tool_rounds),
        ), None

    return None, f"unknown AI_PROVIDER {provider!r}"
