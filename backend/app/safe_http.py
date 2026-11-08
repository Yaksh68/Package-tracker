"""HTTP for URLs that a user or the AI chose: public addresses only, checked
on every request.

Checking a hostname and then letting an HTTP client connect leaves two gaps:
the DNS answer can change in between (DNS rebinding), and a redirect can
point anywhere. `PublicOnlyTransport` closes both:
- for every request it resolves the host itself, and refuses unless all of
  its addresses are public (git_rules.public_addresses);
- it then connects to the address it checked, keeping the hostname for TLS
  (SNI and the certificate check) and for the Host header;
- `public_client()` never follows redirects. Code that does follow them sends
  each hop through the same transport, so each hop is checked again.
"""
from __future__ import annotations

import asyncio

import httpx

from .git_rules import RepoUrlError, public_addresses


class AddressRefused(httpx.ConnectError):
    """The host resolves to an address we won't connect to. The message is
    safe to show the user."""


class PublicOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.raw_host.decode("ascii")
        try:
            address = (await asyncio.to_thread(public_addresses, host))[0]
        except RepoUrlError as e:
            raise AddressRefused(str(e), request=request) from None
        # connect to the checked address; the Host header already names the host
        request.url = request.url.copy_with(host=address)
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def public_client(**kwargs) -> httpx.AsyncClient:
    """An AsyncClient that reaches public addresses only. It doesn't follow
    redirects, and it ignores proxy settings in the environment (a proxy would
    connect on our behalf, past the check)."""
    return httpx.AsyncClient(transport=PublicOnlyTransport(), follow_redirects=False,
                             trust_env=False, **kwargs)
