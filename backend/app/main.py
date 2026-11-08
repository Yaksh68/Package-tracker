"""FastAPI application. On startup: database migrations, the built-in local
user and workspace, encryption of any per-repo tokens still in plain text,
then APScheduler."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import accounts, crypto, db, limits, scan_queue, scheduler, storage
from .api.admin import router as admin_router
from .api.routes import router
from .api.workspaces import router as workspaces_router
from .auth import CsrfMiddleware
from .ratelimit import RateLimitMiddleware
from .auth import router as auth_router
from .config import get_settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("packages-tracker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # bring the schema up to date before anything reads it
    db.upgrade()
    accounts.ensure_local_rows()
    crypto.encrypt("startup check")  # fail fast without APP_SECRET_KEY in the public mode
    if changed := storage.encrypt_plaintext_tokens():
        log.info("encrypted %d per-repo token(s) that were stored in plain text", changed)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()
        scan_queue.shutdown()


app = FastAPI(title="Packages Tracker", version="0.1.0", lifespan=lifespan)

# Vite dev server origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# public mode: data-changing requests must prove they came from our own UI
app.add_middleware(CsrfMiddleware)
# public mode: added last, so it runs first and turns floods away cheaply
app.add_middleware(RateLimitMiddleware)

@app.exception_handler(limits.LimitError)
async def limit_reached(request: Request, exc: limits.LimitError) -> JSONResponse:
    return JSONResponse({"detail": str(exc), "limit": exc.name}, status_code=exc.status)


app.include_router(auth_router)        # sign-in: public by design
app.include_router(router)             # every route below checks rbac.ROUTE_PERMS
app.include_router(workspaces_router)
app.include_router(admin_router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/info")
def info() -> dict:
    """Public facts for pages anyone can open (sign-in, privacy, terms)."""
    s = get_settings()
    return {"mode": "github" if s.public_mode() else "local",
            "operator_name": s.operator_name or None, "contact_email": s.contact_email or None}
