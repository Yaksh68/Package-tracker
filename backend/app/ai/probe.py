"""The Settings page's "Test connection": does the provider accept the key, and
which models can it use? Listing models costs no tokens with any provider we
support, so testing never spends the workspace's credit."""
from __future__ import annotations

from ..config import get_settings
from .config import PRESET_BASE_URLS, AiConfig
from .errors import AiConfigError


async def test_connection(provider: str, *, api_key: str | None, base_url: str | None) -> list[str]:
    """The provider's model IDs (empty for the Claude CLI, which uses its own
    login and model names). Raises AiError, whose message is safe to show,
    when the provider refuses or can't be reached."""
    provider = (provider or "").strip().lower()
    if provider in ("", "claude-cli"):
        if get_settings().public_mode():
            raise AiConfigError("the Claude CLI can't be used in the public mode")
        from ..agents.transport import AgentError, resolve_cli_path
        try:
            resolve_cli_path()
        except AgentError as e:
            raise AiConfigError(str(e)) from e
        return []
    if provider == "anthropic":
        if not api_key:
            raise AiConfigError("add an Anthropic API key first")
        from .anthropic_provider import list_models
        return await list_models(api_key)
    if provider in PRESET_BASE_URLS or provider == "openai-compatible":
        preset = provider if provider in PRESET_BASE_URLS else None
        url = PRESET_BASE_URLS.get(provider) or (base_url or "").strip()
        if not url:
            raise AiConfigError("set the base URL first")
        if preset and not api_key:
            raise AiConfigError(f"add a {provider} API key first")
        from .openai_compat import list_models
        return await list_models(AiConfig(provider="openai-compatible", model="", preset=preset,
                                          api_key=api_key, base_url=url))
    raise AiConfigError(f"unknown AI provider {provider!r}")
