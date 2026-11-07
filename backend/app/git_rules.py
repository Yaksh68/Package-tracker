"""Rules for the repo URLs and branch names users give us. Both reach `git`.

A "URL" such as `--upload-pack=<cmd>` would reach git as an option and run a
command on the server. `file:///…`, or a bare path, would copy server files into
a scan. So both values are checked before git ever sees them, and the clone
step also puts the URL after `--` (see agents/clone.py).

The public mode (`auth_mode=github`) is stricter. It allows https only, the host
must be on the allowlist, IP addresses aren't allowed, and at clone time the
host has to resolve to a public address. The local mode keeps internal git
servers usable: http is allowed, and with no allowlist any host is accepted.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from .config import get_settings


class RepoUrlError(ValueError):
    """A repo URL or branch name was rejected. The message is safe to show the user."""


def host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """`host` matches an allowlist entry exactly, or `*.domain` matches its
    subdomains (not the bare domain)."""
    host = host.lower().rstrip(".")
    for entry in allowed:
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def check_repo_url(url: str) -> str:
    """Return the trimmed URL, or raise RepoUrlError. Makes no network calls."""
    settings = get_settings()
    public = settings.public_mode()
    url = (url or "").strip()
    if not url:
        raise RepoUrlError("enter the repository URL")
    if url.startswith("-") or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise RepoUrlError("the repository URL contains characters git would misread")

    parts = urlsplit(url)
    schemes = ("https",) if public else ("https", "http")
    if parts.scheme.lower() not in schemes:
        raise RepoUrlError("use an https:// URL" if public else "use an https:// or http:// URL")
    if "@" in parts.netloc:
        raise RepoUrlError(
            "don't put credentials in the URL; use the access token field instead"
        )
    try:
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as e:
        raise RepoUrlError(f"the URL's port is invalid: {e}") from e

    host = (parts.hostname or "").lower()
    if not host:
        raise RepoUrlError("the repository URL has no host")
    if public and _is_ip(host):
        raise RepoUrlError("use the server's host name, not an IP address")
    allowed = settings.git_hosts()
    if allowed and not host_allowed(host, allowed):
        raise RepoUrlError(
            f"cloning from {host} isn't allowed here (allowed hosts: {', '.join(allowed)})"
        )
    return url


_BAD_REF_CHARS = set(" ~^:?*[\\")


def check_branch(branch: str) -> str:
    """Return the trimmed branch name, or raise RepoUrlError if git's ref-name
    rules forbid it, or if it could be read as a command-line option."""
    b = (branch or "").strip()
    parts = b.split("/")
    bad = (
        not b
        or b.startswith(("-", "/"))
        or b.endswith(("/", "."))
        or b == "@"
        or ".." in b
        or "//" in b
        or "@{" in b
        or any(ord(c) < 32 or ord(c) == 127 or c in _BAD_REF_CHARS for c in b)
        or any(p.startswith(".") or p.endswith(".lock") for p in parts)
    )
    if bad:
        raise RepoUrlError(f"{branch!r} isn't a valid git branch name")
    return b


def require_public_address(host: str) -> None:
    """Raise RepoUrlError unless every address `host` resolves to is a public
    one. That excludes private, loopback, link-local, CGNAT and reserved
    ranges, so a URL can't be used to reach internal services. For cloning,
    the host allowlist is the main control and this is a second layer behind
    it. The AI's fetch_url tool uses it too. Blocking: call it from a worker
    thread inside async code."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise RepoUrlError(f"could not resolve {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise RepoUrlError(f"{host} resolves to a non-public address ({ip}), which isn't allowed")
