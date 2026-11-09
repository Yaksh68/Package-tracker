"""POST /api/trial: a free scan without an account (phase 7; see trial.py).

Public on purpose, like sign-in: it needs no session and isn't behind
rbac.authorize. It starts a guest session (the same cookie a signed-in user
gets), queues the scan with the visitor's token, which never leaves memory,
and says which repo and branch to look at."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import accounts, scan_queue, trial
from ..auth import session_cookie_name
from ..config import get_settings

router = APIRouter(prefix="/api")


class TrialBody(BaseModel):
    url: str = Field(max_length=500)
    branch: str | None = Field(default=None, max_length=255)
    # a token that can read a private repo: used for this one scan, never stored
    token: str | None = Field(default=None, max_length=500)


@router.post("/trial", status_code=201)
async def start_trial(body: TrialBody, request: Request) -> JSONResponse:
    s = get_settings()
    if not s.public_mode():
        raise HTTPException(404, "free scans are for the public service")
    cookie = request.cookies.get(session_cookie_name())
    current = await asyncio.to_thread(accounts.user_for_session, cookie)
    token = (body.token or "").strip() or None
    try:
        started = await asyncio.to_thread(
            trial.start, body.url, body.branch, token,
            address=request.client.host if request.client else "unknown",
            current_user=current, user_agent=request.headers.get("user-agent"))
    except trial.TrialError as e:
        return JSONResponse({"detail": str(e), **e.extra}, status_code=e.status)
    scan_queue.enqueue_trial(started.repo, started.branch, token)
    resp = JSONResponse({"repo_id": started.repo.id, "branch": started.branch, "hours": started.hours},
                        status_code=201)
    resp.set_cookie(session_cookie_name(), started.session_token, max_age=started.hours * 3600,
                    httponly=True, secure=s.https(), samesite="lax", path="/")
    return resp
