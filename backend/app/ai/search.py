"""Web search and page fetching that the app runs itself, for AI providers
with no built-in search (the OpenAI-compatible ones).

Search goes through Tavily or Exa with the workspace's own key. `fetch_text`
fetches only public http(s) addresses: every redirect hop is re-checked, and
size and time are capped. A model can't use it to reach internal services, and
a huge page can't exhaust memory.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from ..git_rules import RepoUrlError, require_public_address
from .errors import AiAuthError, AiProviderError, AiQuotaError

_TIMEOUT = httpx.Timeout(20.0)
_FETCH_TIMEOUT = httpx.Timeout(15.0)
_FETCH_MAX_BYTES = 5 * 1024 * 1024
_FETCH_MAX_CHARS = 12_000
_SNIPPET_CHARS = 500


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str

    def as_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "snippet": self.snippet}


def _raise_for(service: str, r: httpx.Response) -> None:
    if r.status_code in (401, 403):
        raise AiAuthError(f"{service} rejected the search API key ({r.status_code})")
    body = r.text[:300].lower()
    if r.status_code == 429 or any(w in body for w in ("limit", "quota", "credit")):
        raise AiQuotaError(f"{service} search limit reached ({r.status_code})")
    raise AiProviderError(f"{service} search failed ({r.status_code})")


class TavilySearch:
    """POST https://api.tavily.com/search with `Authorization: Bearer <key>`."""
    name = "tavily"

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._key, self._client = api_key, client

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        try:
            r = await self._client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self._key}"},
                json={"query": query, "max_results": limit, "search_depth": "basic"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise AiProviderError(f"tavily search unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            _raise_for("tavily", r)
        out = []
        for item in (r.json().get("results") or [])[:limit]:
            if isinstance(item, dict) and item.get("url"):
                out.append(SearchHit(url=item["url"], title=str(item.get("title") or ""),
                                     snippet=str(item.get("content") or "")[:_SNIPPET_CHARS]))
        return out


class ExaSearch:
    """POST https://api.exa.ai/search with `x-api-key: <key>`."""
    name = "exa"

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._key, self._client = api_key, client

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        try:
            r = await self._client.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": self._key},
                json={"query": query, "numResults": limit, "contents": {"highlights": True}},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise AiProviderError(f"exa search unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            _raise_for("exa", r)
        out = []
        for item in (r.json().get("results") or [])[:limit]:
            if not (isinstance(item, dict) and item.get("url")):
                continue
            highlights = item.get("highlights") or []
            snippet = " … ".join(h for h in highlights if isinstance(h, str)) or str(item.get("text") or "")
            out.append(SearchHit(url=item["url"], title=str(item.get("title") or ""),
                                 snippet=snippet[:_SNIPPET_CHARS]))
        return out


def make_search(provider: str, api_key: str, client: httpx.AsyncClient):
    if provider == "tavily":
        return TavilySearch(api_key, client)
    if provider == "exa":
        return ExaSearch(api_key, client)
    raise ValueError(f"unknown search provider {provider!r}")


# --------------------------------------------------------------------------- #
# fetch_url                                                                     #
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()


class FetchRefused(Exception):
    """The URL isn't one we'll fetch. The message is returned to the model."""


async def _check_public(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        raise FetchRefused("only plain http(s) URLs can be fetched")
    host = parts.hostname.lower()
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise FetchRefused("URLs with IP-address hosts can't be fetched")
    except ValueError:
        pass
    try:
        await asyncio.to_thread(require_public_address, host)
    except RepoUrlError as e:
        raise FetchRefused(str(e)) from e


async def fetch_text(url: str, client: httpx.AsyncClient, *, max_redirects: int = 3) -> tuple[str, str]:
    """Return (final_url, text) for a public web page, following at most
    `max_redirects` redirects. Every hop is checked. Raises FetchRefused for
    a URL we won't fetch, and httpx errors for network failures."""
    current = url
    for _ in range(max_redirects + 1):
        await _check_public(current)
        async with client.stream("GET", current, timeout=_FETCH_TIMEOUT,
                                 follow_redirects=False) as r:
            if r.is_redirect and r.headers.get("location"):
                current = urljoin(current, r.headers["location"])
                continue
            if r.status_code != 200:
                raise FetchRefused(f"the page returned HTTP {r.status_code}")
            kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind and not (kind.startswith("text/") or kind in ("application/json", "application/xhtml+xml")):
                raise FetchRefused(f"not a text page ({kind})")
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body.extend(chunk)
                if len(body) > _FETCH_MAX_BYTES:
                    break
            text = body.decode(r.encoding or "utf-8", errors="replace")
            if "html" in kind or "<html" in text[:500].lower():
                text = html_to_text(text)
            return current, text[:_FETCH_MAX_CHARS]
    raise FetchRefused("too many redirects")
