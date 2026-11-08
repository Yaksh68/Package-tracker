"""Who's calling: GitHub sign-in, sessions, and the request's principal
(user and active workspace).

Local mode (AUTH_MODE=none): no login. Every request is the built-in local
user, working in the local workspace, exactly as before accounts existed.

Public mode (AUTH_MODE=github):
- Sign in with GitHub, the OAuth web flow, written directly on httpx.
  /api/auth/github/login redirects to GitHub with a random `state`, which
  is also kept in a short-lived HttpOnly cookie.
  /api/auth/github/callback checks that state, swaps the code for a token,
  reads the profile, creates or updates the user (with a personal workspace
  on first sign-in) and starts a session. No OAuth scopes are requested:
  the token only reads public data, but it gives each user their own GitHub
  API rate limit.
- Sessions use a random token in an HttpOnly, SameSite=Lax cookie (Secure
  and `__Host-` prefixed on https). Only a hash of it is stored.
  (SameSite=Strict would drop the cookie on the redirect back from GitHub.)
- Requests that change data must send `X-Requested-With: packages-tracker`,
  plus a matching Origin when the browser sends one (CsrfMiddleware).
  Another site can't send that header without a CORS preflight, which is
  never allowed.
- Sign-up is capped at MAX_USERS. Platform admins (PLATFORM_ADMINS) can
  always sign in; disabled users can't.
- The active workspace comes from the X-Workspace header, defaulting to the
  user's personal workspace. A workspace the user isn't in is a 404.
"""
from __future__ import annotations

import asyncio
import html
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import accounts, storage
from .config import get_settings
from .models import LOCAL_WORKSPACE_ID, User, Workspace

_AUTHORIZE = "https://github.com/login/oauth/authorize"
_TOKEN = "https://github.com/login/oauth/access_token"
_USER = "https://api.github.com/user"
STATE_COOKIE = "pt_oauth_state"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "packages-tracker"


@dataclass
class Principal:
    user: User
    workspace_id: str
    role: str
    workspaces: list[Workspace] = field(default_factory=list)

    def member_of(self, workspace_id: str | None) -> bool:
        if not get_settings().public_mode():
            return True
        return any(w.id == (workspace_id or LOCAL_WORKSPACE_ID) for w in self.workspaces)


def session_cookie_name() -> str:
    return "__Host-pt_session" if get_settings().https() else "pt_session"


def _local_principal() -> Principal:
    ws = Workspace(id=LOCAL_WORKSPACE_ID, name="Local workspace", personal=True, role="owner")
    return Principal(user=accounts.local_user(), workspace_id=LOCAL_WORKSPACE_ID, role="owner",
                     workspaces=[ws])


def get_principal(request: Request) -> Principal:
    """FastAPI dependency: the caller and the workspace they're working in."""
    if not get_settings().public_mode():
        return _local_principal()
    user = accounts.user_for_session(request.cookies.get(session_cookie_name()))
    if user is None:
        raise HTTPException(401, "sign in first")
    accounts.touch_activity(user.id)
    workspaces = accounts.user_workspaces(user.id)
    wanted = request.headers.get("x-workspace")
    active = next((w for w in workspaces if w.id == wanted), None) if wanted else None
    if wanted and active is None:
        raise HTTPException(404, "workspace not found")
    active = active or (workspaces[0] if workspaces else None)
    if active is None:
        raise HTTPException(403, "you don't belong to any workspace")
    return Principal(user=user, workspace_id=active.id, role=active.role or "viewer",
                     workspaces=workspaces)


# --------------------------------------------------------------------------- #
# CSRF                                                                          #
# --------------------------------------------------------------------------- #
def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()


class CsrfMiddleware(BaseHTTPMiddleware):
    """Public mode: refuse data-changing API requests that a browser could
    have sent from another site."""

    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        if (s.public_mode() and request.method not in ("GET", "HEAD", "OPTIONS")
                and request.url.path.startswith("/api/")):
            if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return JSONResponse({"detail": "missing the X-Requested-With header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/").lower() != _origin(s.public_base_url):
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        return await call_next(request)


# --------------------------------------------------------------------------- #
# GitHub sign-in                                                                #
# --------------------------------------------------------------------------- #
router = APIRouter(prefix="/api")


def _github_client() -> httpx.AsyncClient:
    """The HTTP client for GitHub's OAuth endpoints (tests replace it)."""
    return httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "packages-tracker"})


def _callback_url() -> str:
    return get_settings().public_base_url.rstrip("/") + "/api/auth/github/callback"


def _page(status: int, message: str) -> HTMLResponse:
    body = (f"<!doctype html><meta charset=utf-8><title>Packages Tracker</title>"
            f"<p>{html.escape(message)}</p><p><a href='/'>Back to Packages Tracker</a></p>")
    return HTMLResponse(body, status_code=status)


def _public_only() -> None:
    if not get_settings().public_mode():
        raise HTTPException(404, "there's no sign-in in the local mode")


@router.get("/auth/github/login")
def github_login() -> RedirectResponse:
    _public_only()
    s = get_settings()
    if not (s.github_oauth_client_id and s.github_oauth_client_secret):
        raise HTTPException(503, "GitHub sign-in isn't configured on this server")
    state = secrets.token_urlsafe(24)
    query = urlencode({"client_id": s.github_oauth_client_id, "redirect_uri": _callback_url(),
                       "state": state, "allow_signup": "true"})
    resp = RedirectResponse(f"{_AUTHORIZE}?{query}", status_code=302)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=s.https(),
                    samesite="lax", path="/api/auth")
    return resp


async def _exchange(code: str) -> tuple[str, dict] | None:
    """Swap the OAuth code for a token and read the user's GitHub profile."""
    s = get_settings()
    async with _github_client() as client:
        r = await client.post(_TOKEN, headers={"Accept": "application/json"}, data={
            "client_id": s.github_oauth_client_id, "client_secret": s.github_oauth_client_secret,
            "code": code, "redirect_uri": _callback_url(),
        })
        token = r.json().get("access_token") if r.status_code == 200 else None
        if not token:
            return None
        u = await client.get(_USER, headers={"Authorization": f"Bearer {token}",
                                             "Accept": "application/vnd.github+json"})
        if u.status_code != 200:
            return None
        profile = u.json()
    if not isinstance(profile, dict) or not profile.get("id") or not profile.get("login"):
        return None
    return token, profile


@router.get("/auth/github/callback")
async def github_callback(request: Request, code: str | None = None, state: str | None = None,
                          error: str | None = None):
    _public_only()
    s = get_settings()
    expected = request.cookies.get(STATE_COOKIE)
    if error:
        return _page(400, "GitHub sign-in was cancelled.")
    if not (code and state and expected and secrets.compare_digest(state, expected)):
        return _page(400, "That sign-in link has expired or isn't valid. Please try again.")
    try:
        got = await _exchange(code)
    except httpx.HTTPError:
        got = None
    if got is None:
        return _page(502, "GitHub didn't complete the sign-in. Please try again.")
    token, profile = got
    admin = str(profile["login"]).lower() in s.admin_logins()

    existing = await asyncio.to_thread(accounts.find_github_user, int(profile["id"]))
    if existing is not None and existing.status != "active":
        return _page(403, "This account is disabled.")
    if (existing is None and not admin
            and await asyncio.to_thread(accounts.count_github_users) >= s.max_users):
        return _page(403, "Sign-ups are full right now. Please try again later.")

    user, created = await asyncio.to_thread(accounts.sign_in_github_user, profile, token, admin=admin)
    if admin and await asyncio.to_thread(storage.load_repos, LOCAL_WORKSPACE_ID):
        # repos from before the public mode live in the local workspace: the
        # platform admins get them
        await asyncio.to_thread(accounts.add_member, LOCAL_WORKSPACE_ID, user.id, "owner")
    accounts.record_event("sign_up" if created else "sign_in", user_id=user.id)
    session_token = await asyncio.to_thread(accounts.create_session, user.id,
                                            request.headers.get("user-agent"))
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(session_cookie_name(), session_token,
                    max_age=s.session_days * 86400, httponly=True, secure=s.https(),
                    samesite="lax", path="/")
    resp.delete_cookie(STATE_COOKIE, path="/api/auth")
    return resp


@router.post("/auth/logout", status_code=204)
def logout(request: Request):
    accounts.end_session(request.cookies.get(session_cookie_name()))
    resp = JSONResponse(None, status_code=204)
    resp.delete_cookie(session_cookie_name(), path="/")
    return resp
