"""Database engine + session helpers (SQLAlchemy 2.0, sync).

PostgreSQL in every real deployment (`DATABASE_URL=postgresql+psycopg://…`);
the unit tests point it at a throwaway SQLite file. Code that needs a
Postgres-only feature must check `is_postgres()` and degrade on SQLite.

Schema changes go through Alembic (`alembic/versions/`); `upgrade()` applies
them and runs on every startup, so a deploy never serves an old schema.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import get_settings

# Postgres `json` keeps the text as written — object key order included — which
# matters wherever order is user-visible (flag-count tags, scan-log entries,
# image tags). JSONB reorders keys, so it's only used for documents that are
# always parsed back through Pydantic and may be queried (enrichment data).
JSONType = JSON()
JSONBType = JSON().with_variant(JSONB(), "postgresql")

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at the tracker's PostgreSQL database, e.g. "
            "postgresql+psycopg://packages:<password>@127.0.0.1:5432/packages_tracker (see README)."
        )
    return url


@lru_cache
def get_engine() -> Engine:
    url = database_url()
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # SQLite ignores ON DELETE CASCADE without this
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


def is_postgres() -> bool:
    return get_engine().dialect.name == "postgresql"


@contextmanager
def session() -> Iterator[Session]:
    """One transaction: commits on success, rolls back on any exception."""
    with Session(get_engine(), expire_on_commit=False) as s, s.begin():
        yield s


def reset_engine() -> None:
    """Drop the cached engine (tests switch DATABASE_URL between cases)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()


def alembic_config():
    from alembic.config import Config

    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    cfg.attributes["configure_logger"] = False  # keep the app's logging setup
    return cfg


def upgrade() -> None:
    """Apply pending migrations (`alembic upgrade head`)."""
    from alembic import command

    command.upgrade(alembic_config(), "head")
