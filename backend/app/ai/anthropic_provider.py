"""Research through the Anthropic API with the workspace's key, using Claude's
server-side web_search and web_fetch tools (official `anthropic` SDK).

- Tool versions depend on the model. The `_20260209` variants (dynamic
  filtering) need Opus 4.6+ or Sonnet 4.6+; Haiku and older models use the
  basic `web_search_20250305` / `web_fetch_20250910`.
- A long server-side tool loop can stop with `pause_turn`. The paused turn
  is sent back so the server continues it (capped).
- `refusal` fails the batch (the structured verdicts stay). Models that
  support it (Fable 5.1, Opus 5.5 / 5, Sonnet 5.5) send
  `fallbacks="default"`, so the API retries a declined request on a
  fallback model by itself. Haiku has no server-side fallback.
- Every search result and fetched URL is collected, so the citation check
  can hold the model to sources it actually saw.
- The API doesn't return a cost; pricing.claude_cost estimates it from usage.
"""
from __future__ import annotations

import asyncio

import anthropic

from .config import AiConfig
from .errors import AiAuthError, AiConfigError, AiError, AiProviderError, AiQuotaError, AiTimeoutError, redact_keys
from .pricing import claude_cost
from .result import AgentResult, log_tool_result, log_tool_use

# The models documented for the `_20260209` web tools. Every other model,
# Haiku included, gets the basic versions.
_DYNAMIC_FILTER_MODELS = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6",
)
# Models that accept server-side refusal fallbacks (`fallbacks="default"`).
_FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_MAX_RESUMES = 4      # pause_turn continuations per batch
_MAX_TOKENS = 16000   # the answer is a small JSON object; leave room for thinking


def web_tools(model: str, max_uses: int) -> list[dict]:
    newer = model.startswith(_DYNAMIC_FILTER_MODELS)
    return [
        {"type": "web_search_20260209" if newer else "web_search_20250305",
         "name": "web_search", "max_uses": max_uses},
        {"type": "web_fetch_20260209" if newer else "web_fetch_20250910",
         "name": "web_fetch", "max_uses": max_uses},
    ]


def _map_error(e: Exception, model: str) -> AiError:
    msg = redact_keys(str(getattr(e, "message", None) or e))[:300]
    if isinstance(e, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return AiAuthError(f"Anthropic rejected the API key: {msg}")
    if isinstance(e, anthropic.RateLimitError):
        return AiQuotaError(f"Anthropic rate limit: {msg}")
    if isinstance(e, anthropic.NotFoundError):
        return AiConfigError(f"model {model!r} isn't available to this key: {msg}")
    if isinstance(e, anthropic.BadRequestError):
        if "credit balance" in msg.lower():
            return AiQuotaError(f"Anthropic credit balance too low: {msg}")
        return AiProviderError(f"Anthropic rejected the request: {msg}")
    if isinstance(e, anthropic.APITimeoutError):
        return AiTimeoutError("the Anthropic API timed out")
    if isinstance(e, anthropic.APIConnectionError):
        return AiProviderError("could not reach the Anthropic API")
    return AiProviderError(f"Anthropic API error: {msg}")


async def _send(client, model: str, system: str, messages: list, tools: list):
    kwargs = dict(model=model, max_tokens=_MAX_TOKENS, system=system, messages=messages, tools=tools)
    if model.startswith(_FALLBACK_MODELS):
        async with client.beta.messages.stream(**kwargs, betas=[_FALLBACK_BETA], fallbacks="default") as stream:
            return await stream.get_final_message()
    async with client.messages.stream(**kwargs) as stream:
        return await stream.get_final_message()


def _add_usage(total: dict, usage) -> None:
    if usage is None:
        return
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        total[k] = total.get(k, 0) + int(getattr(usage, k, 0) or 0)
    srv = getattr(usage, "server_tool_use", None)
    if srv is not None:
        t = total.setdefault("server_tool_use", {})
        for k in ("web_search_requests", "web_fetch_requests"):
            t[k] = t.get(k, 0) + int(getattr(srv, k, 0) or 0)


def _scan_blocks(content, seen: set[str]) -> None:
    """Log the server-side tool calls (Activity page) and collect every URL
    that search and fetch results returned."""
    for b in content or []:
        kind = getattr(b, "type", None)
        if kind == "server_tool_use":
            log_tool_use(getattr(b, "name", "tool"), getattr(b, "input", {}))
        elif kind == "web_search_tool_result":
            results = getattr(b, "content", None)
            if isinstance(results, list):
                urls = [r.url for r in results if getattr(r, "url", None)]
                seen.update(urls)
                log_tool_result([f"{getattr(r, 'title', '')} — {r.url}" for r in results[:8]
                                 if getattr(r, "url", None)])
            else:
                log_tool_result(f"web_search error: {getattr(results, 'error_code', results)}", is_error=True)
        elif kind == "web_fetch_tool_result":
            fetched = getattr(b, "content", None)
            if getattr(fetched, "type", None) == "web_fetch_result" and getattr(fetched, "url", None):
                seen.add(fetched.url)
                log_tool_result(f"fetched {fetched.url}")
            else:
                log_tool_result(f"web_fetch error: {getattr(fetched, 'error_code', fetched)}", is_error=True)


async def _research(client, cfg: AiConfig, prompt: str, system: str, max_searches: int) -> AgentResult:
    tools = web_tools(cfg.model, max_searches)
    messages: list = [{"role": "user", "content": prompt}]
    usage: dict = {}
    seen: set[str] = set()
    turns = 0
    msg = None
    for _ in range(_MAX_RESUMES + 1):
        msg = await _send(client, cfg.model, system, messages, tools)
        turns += 1
        _add_usage(usage, msg.usage)
        _scan_blocks(msg.content, seen)
        if msg.stop_reason != "pause_turn":
            break
        # the server's tool loop hit its iteration cap: send the paused turn
        # back unchanged and the server resumes where it stopped
        messages.append({"role": "assistant", "content": msg.content})
    if msg.stop_reason == "refusal":
        raise AiProviderError("the model declined to answer (refusal)")
    text = "\n".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    return AgentResult(text=text, num_turns=turns, cost_usd=claude_cost(cfg.model, usage),
                       usage=usage, seen_urls=seen)


async def research(cfg: AiConfig, prompt: str, system: str, *, max_searches: int,
                   timeout: float) -> AgentResult:
    client = anthropic.AsyncAnthropic(api_key=cfg.api_key, timeout=timeout, max_retries=2)
    try:
        # the SDK times out each HTTP request; this caps the whole conversation
        return await asyncio.wait_for(_research(client, cfg, prompt, system, max_searches),
                                      timeout=timeout * 2)
    except asyncio.TimeoutError as e:
        raise AiTimeoutError(f"the research took longer than {timeout * 2:.0f}s") from e
    except anthropic.APIError as e:
        raise _map_error(e, cfg.model) from e
    finally:
        await client.close()


async def list_models(api_key: str) -> list[str]:
    """Model IDs this key can use (GET /v1/models: no token cost). Used by the
    "Test connection" button; it also proves the key works."""
    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=20.0, max_retries=1)
    try:
        return [m.id async for m in client.models.list(limit=100)]
    except anthropic.APIError as e:
        raise _map_error(e, "") from e
    finally:
        await client.close()
