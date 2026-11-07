"""Estimated cost of one research call. The Anthropic API (unlike the Claude CLI)
doesn't return a cost, so it's computed from the usage it does return.

Prices are Anthropic's first-party list prices per million tokens, as of
2026-10. Check them before relying on a number; they're estimates for the scan
log and the analytics, not a bill. Any model not listed gives None ("not
estimated"); it's never guessed.
"""
from __future__ import annotations

# model prefix -> (input, output, cache read, 5-minute cache write) USD per MTok
_CLAUDE: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),  # prompts up to 100K tokens; see below
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
}
# Claude Haiku 5.5 has a second rate card for prompts over 100K tokens.
_HAIKU_55_LONG = (0.50, 2.50, 0.05, 0.625)
WEB_SEARCH_USD = 10.0 / 1000  # $10 per 1,000 searches; web fetch isn't billed beyond tokens


def _rates(model: str) -> tuple[float, float, float, float] | None:
    # longest matching prefix, so "claude-sonnet-5-5" isn't priced as "claude-sonnet-5"
    for prefix in sorted(_CLAUDE, key=len, reverse=True):
        if model.startswith(prefix):
            return _CLAUDE[prefix]
    return None


def claude_cost(model: str, usage: dict) -> float | None:
    """USD for one Anthropic API call's `usage` (summed over pause/resume turns)."""
    rates = _rates(model)
    if rates is None:
        return None
    fresh = int(usage.get("input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    if model.startswith("claude-haiku-5-5") and fresh + read + write > 100_000:
        rates = _HAIKU_55_LONG
    p_in, p_out, p_read, p_write = rates
    searches = int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)
    tokens = (fresh * p_in + out * p_out + read * p_read + write * p_write) / 1_000_000
    return round(tokens + searches * WEB_SEARCH_USD, 6)


def openai_compatible_cost(model: str) -> float | None:
    """We don't track third-party prices: a `:free` model costs nothing, and
    anything else is "not estimated". The search service's own charges aren't
    counted either."""
    return 0.0 if model.endswith(":free") else None
