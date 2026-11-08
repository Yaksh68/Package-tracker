"""Research through any OpenAI-compatible API (OpenAI, Gemini, Groq,
OpenRouter, or a custom URL such as Mistral, GitHub Models or a local
Ollama) with the official `openai` SDK.

These providers have no built-in web search we can count on, so the app runs
two tools itself:
- web_search(query): Tavily or Exa, with the workspace's search key;
- fetch_url(url): only URLs the searches returned, and only public addresses.

Every URL those tools return is recorded, and the citation check
(citations.enforce) drops any source the model cites outside that set.

In the public mode a custom base URL is reached through
safe_http.public_client(), so it can't point the server at internal
addresses, by DNS or by redirect. Page fetches always go through it.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx
import openai

from ..config import get_settings
from ..safe_http import AddressRefused, public_client
from .citations import normalize
from .config import AiConfig
from .errors import AiAuthError, AiConfigError, AiError, AiProviderError, AiQuotaError, AiTimeoutError, redact_keys
from .pricing import openai_compatible_cost
from .result import AgentResult, log_tool_result, log_tool_use
from .search import FetchRefused, fetch_text, make_search

_TOOL_RESULT_CHARS = 16_000
_SEARCH_RESULTS = 5
_UA = {"User-Agent": "packages-tracker/0.1"}

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Returns up to 5 results, each with url, title and snippet.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "The search query"}},
                       "required": ["query"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": ("Fetch the text of a page that web_search returned. "
                        "Only URLs from web_search results are accepted."),
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string", "description": "A URL from web_search results"}},
                       "required": ["url"], "additionalProperties": False},
    }},
]

_TOOL_RULES = (
    "You have two tools: web_search(query) and fetch_url(url). fetch_url only accepts "
    "URLs that web_search returned. Cite ONLY URLs these tools returned to you; any "
    "other citation is discarded. When you have finished researching, reply with only "
    "the JSON object."
)


def _map_error(e: Exception, cfg: AiConfig) -> AiError:
    who = cfg.preset or urlsplit(cfg.base_url or "").hostname or "the provider"
    msg = redact_keys(str(getattr(e, "message", None) or e))[:300]
    if isinstance(e, openai.APIConnectionError) and isinstance(e.__cause__, AddressRefused):
        return AiConfigError(f"the base URL can't be used: {e.__cause__}")
    if isinstance(e, openai.AuthenticationError | openai.PermissionDeniedError):
        return AiAuthError(f"{who} rejected the API key: {msg}")
    if isinstance(e, openai.RateLimitError):
        return AiQuotaError(f"{who} rate limit or quota reached: {msg}")
    if isinstance(e, openai.NotFoundError):
        return AiConfigError(f"model {cfg.model!r} wasn't found at {who}: {msg}")
    if isinstance(e, openai.BadRequestError):
        low = msg.lower()
        if "tool" in low and ("support" in low or "not available" in low):
            return AiConfigError(f"model {cfg.model!r} at {who} doesn't support tool calling")
        return AiProviderError(f"{who} rejected the request: {msg}")
    if isinstance(e, openai.APITimeoutError):
        return AiTimeoutError(f"{who} timed out")
    if isinstance(e, openai.APIConnectionError):
        return AiProviderError(f"could not reach {who}")
    return AiProviderError(f"{who} API error: {msg}")


class _Session:
    """One research conversation's tool state."""

    def __init__(self, search, http: httpx.AsyncClient, max_searches: int) -> None:
        self.search, self.http, self.max_searches = search, http, max_searches
        self.searches = 0
        self.fetches = 0
        self.seen: set[str] = set()

    def _seen(self, url: str) -> bool:
        return normalize(url) in {normalize(u) for u in self.seen}

    async def run(self, name: str, raw_args: str | None) -> dict:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"error": "the tool arguments weren't valid JSON"}
        if not isinstance(args, dict):
            return {"error": "the tool arguments must be a JSON object"}
        if name == "web_search":
            query = str(args.get("query") or "").strip()
            if not query:
                return {"error": "query is required"}
            if self.searches >= self.max_searches:
                return {"error": f"the search limit ({self.max_searches}) is reached; answer with what you have"}
            log_tool_use("web_search", {"query": query})
            hits = await self.search.search(query, limit=_SEARCH_RESULTS)  # auth/quota errors end the batch
            self.searches += 1
            self.seen.update(h.url for h in hits)
            log_tool_result([f"{h.title} — {h.url}" for h in hits])
            return {"results": [h.as_dict() for h in hits]}
        if name == "fetch_url":
            url = str(args.get("url") or "").strip()
            if not url or not self._seen(url):
                return {"error": "only URLs returned by web_search can be fetched"}
            log_tool_use("fetch_url", {"url": url})
            try:
                final, text = await fetch_text(url, self.http)
            except FetchRefused as e:
                log_tool_result(str(e), is_error=True)
                return {"error": str(e)}
            except httpx.HTTPError as e:
                log_tool_result(f"fetch failed: {type(e).__name__}", is_error=True)
                return {"error": f"couldn't fetch the page ({type(e).__name__})"}
            self.fetches += 1
            self.seen.add(final)
            log_tool_result(f"fetched {final} ({len(text)} chars)")
            return {"url": final, "text": text}
        return {"error": f"unknown tool {name!r}"}


def _add_usage(total: dict, usage) -> None:
    if usage is None:
        return
    total["input_tokens"] = total.get("input_tokens", 0) + int(getattr(usage, "prompt_tokens", 0) or 0)
    total["output_tokens"] = total.get("output_tokens", 0) + int(getattr(usage, "completion_tokens", 0) or 0)


async def _research(client, session: _Session, cfg: AiConfig, prompt: str, system: str) -> AgentResult:
    messages: list = [
        {"role": "system", "content": f"{system}\n\n{_TOOL_RULES}".strip()},
        {"role": "user", "content": prompt},
    ]
    usage: dict = {}
    text = ""
    rounds = 0
    for rounds in range(1, cfg.max_tool_rounds + 2):
        last = rounds > cfg.max_tool_rounds  # out of tool rounds: ask for the answer
        resp = await client.chat.completions.create(
            model=cfg.model, messages=messages, tools=TOOLS,
            tool_choice="none" if last else "auto",
        )
        _add_usage(usage, getattr(resp, "usage", None))
        if not resp.choices:
            raise AiProviderError("the provider returned no answer")
        message = resp.choices[0].message
        calls = list(message.tool_calls or [])
        if not calls or last:
            text = message.content or ""
            break
        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                           for c in calls],
        })
        for c in calls:
            out = await session.run(c.function.name, c.function.arguments)
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps(out)[:_TOOL_RESULT_CHARS]})
    usage["server_tool_use"] = {"web_search_requests": session.searches,
                                "web_fetch_requests": session.fetches}
    return AgentResult(text=text.strip(), num_turns=rounds, cost_usd=openai_compatible_cost(cfg.model),
                       usage=usage, seen_urls=set(session.seen))


def _client(cfg: AiConfig, timeout: float) -> openai.AsyncOpenAI:
    """The SDK client. A custom base URL in the public mode is a URL a user
    typed, so it goes through the public-only transport; the presets' URLs
    are our own constants."""
    guarded = cfg.preset is None and get_settings().public_mode()
    return openai.AsyncOpenAI(api_key=cfg.api_key or "not-needed", base_url=cfg.base_url,
                              timeout=timeout, max_retries=1,
                              http_client=public_client(timeout=timeout) if guarded else None)


async def research(cfg: AiConfig, prompt: str, system: str, *, max_searches: int,
                   timeout: float) -> AgentResult:
    client = _client(cfg, timeout)
    # search APIs are fixed hosts; pages are whatever the search returned
    async with httpx.AsyncClient(headers=_UA) as http, public_client(headers=_UA) as pages:
        session = _Session(make_search(cfg.search_provider, cfg.search_api_key, http), pages, max_searches)
        try:
            return await asyncio.wait_for(_research(client, session, cfg, prompt, system),
                                          timeout=timeout * 2)
        except asyncio.TimeoutError as e:
            raise AiTimeoutError(f"the research took longer than {timeout * 2:.0f}s") from e
        except openai.APIError as e:
            raise _map_error(e, cfg) from e
        finally:
            await client.close()


# ids that are plainly not chat models (embeddings, speech, images, moderation)
_NOT_CHAT = ("embed", "tts", "whisper", "dall-e", "moderation", "transcribe", "audio",
             "realtime", "image", "imagen", "veo", "davinci", "babbage", "aqa")


async def list_models(cfg: AiConfig) -> list[str]:
    """Chat model IDs at this endpoint (GET /models, which costs no tokens).
    Used by "Test connection".
    - OpenRouter lists models without checking the key, so its key is
      checked first (GET /key).
    - Where the listing says which models take tools (OpenRouter does), only
      those are kept: research needs tool calling. Free ones come first.
    """
    client = _client(cfg, 20.0)
    try:
        if cfg.preset == "openrouter":
            await client.get("/key", cast_to=object)  # 401 for a bad key
        models = [m async for m in client.models.list()]
    except openai.APIError as e:
        raise _map_error(e, cfg) from e
    finally:
        await client.close()
    if any(getattr(m, "supported_parameters", None) is not None for m in models):
        models = [m for m in models if "tools" in (getattr(m, "supported_parameters", None) or ())]
    ids = sorted({m.id for m in models if not any(w in m.id.lower() for w in _NOT_CHAT)})
    return sorted(ids, key=lambda i: not i.endswith(":free"))
