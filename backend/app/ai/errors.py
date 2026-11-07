"""Typed AI failures. Each one maps to an `ai_status` and decides whether the
remaining research batches are worth trying (see enrichment_agent)."""
from __future__ import annotations

import re


class AiError(RuntimeError):
    """Base class. `kind` is the enrichment agent's error kind."""
    kind = "error"


class AiAuthError(AiError):
    """The provider rejected the key (or the CLI isn't logged in)."""
    kind = "auth"


class AiQuotaError(AiError):
    """A rate limit, quota or credit balance stopped the call."""
    kind = "quota"


class AiTimeoutError(AiError):
    kind = "timeout"


class AiConfigError(AiError):
    """The AI isn't usable as configured: no provider, unknown model, a
    provider that can't do what was asked."""
    kind = "config"


class AiProviderError(AiError):
    """Anything else the provider returned (5xx, refusal, bad request)."""
    kind = "error"


# Key-shaped strings, scrubbed from every error message before it's logged or
# stored. Providers mask keys in their own errors, but we don't rely on that.
_KEYISH = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{6,}|sk-[A-Za-z0-9_\-]{12,}|tvly-[A-Za-z0-9_\-]{8,}"
    r"|AIza[0-9A-Za-z_\-]{20,}|gsk_[A-Za-z0-9]{12,}|exa-[A-Za-z0-9_\-]{8,})"
)


def redact_keys(text: str) -> str:
    return _KEYISH.sub("***", text)
