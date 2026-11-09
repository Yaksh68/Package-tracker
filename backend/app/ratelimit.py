"""Rate limits (phase 4), in memory, in the public mode only.

- Sign-in (/api/auth/github/...): RATE_LIMIT_SIGNIN a minute per address.
- The API: RATE_LIMIT_SESSION a minute per session, and RATE_LIMIT_ADDRESS a
  minute per address whatever the cookie (so made-up cookies can't each get
  a fresh allowance). Calls without a session get RATE_LIMIT_ANONYMOUS a
  minute per address. /api/health is exempt.

Over a limit: 429 with Retry-After. The counts are sliding windows kept in
memory and never written anywhere, so addresses aren't stored. Behind a
reverse proxy, uvicorn's --proxy-headers makes request.client the visitor's
address (the deploy sets that up).
"""
from __future__ import annotations

import hashlib
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import session_cookie_name
from .config import get_settings

WINDOW = 60.0  # seconds


class SlidingWindow:
    """At most `limit` hits per key in any `window` seconds (WINDOW unless
    given)."""

    def __init__(self, window: float = WINDOW) -> None:
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def hit(self, key: str, limit: int) -> int | None:
        """Count a hit. None when it's allowed; otherwise the seconds until
        it would be."""
        now = time.monotonic()
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= limit:
            return max(1, int(q[0] + self.window - now) + 1)
        q.append(now)
        self._calls += 1
        if self._calls % 1000 == 0:
            self._forget(now)
        return None

    def _forget(self, now: float) -> None:
        """Drop keys idle for a whole window, so memory stays small."""
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]:
            del self._hits[key]


_signin = SlidingWindow()
_sessions = SlidingWindow()
_addresses = SlidingWindow()
_anonymous = SlidingWindow()


def reset() -> None:
    """Forget every count (tests)."""
    global _signin, _sessions, _addresses, _anonymous
    _signin, _sessions, _addresses, _anonymous = SlidingWindow(), SlidingWindow(), SlidingWindow(), SlidingWindow()


def _too_many(wait: int) -> JSONResponse:
    return JSONResponse({"detail": "Too many requests. Wait a moment and try again."},
                        status_code=429, headers={"Retry-After": str(wait)})


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        path = request.url.path
        if s.public_mode() and path.startswith("/api/") and path != "/api/health":
            address = request.client.host if request.client else "unknown"
            if path.startswith("/api/auth/github/"):
                wait = _signin.hit(address, s.rate_limit_signin)
            else:
                cookie = request.cookies.get(session_cookie_name())
                if cookie:
                    # a hash: the session token itself isn't kept anywhere else
                    session_key = hashlib.sha256(cookie.encode()).hexdigest()[:24]
                    wait = (_addresses.hit(address, s.rate_limit_address)
                            or _sessions.hit(session_key, s.rate_limit_session))
                else:
                    wait = _anonymous.hit(address, s.rate_limit_anonymous)
            if wait:
                return _too_many(wait)
        return await call_next(request)
