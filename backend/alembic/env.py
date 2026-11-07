"""Alembic environment: connection from DATABASE_URL, schema from app.db_models."""
from logging.config import fileConfig

from alembic import context

from app.db import get_engine
from app.db_models import Base

config = context.config

# Only the CLI configures logging; when the app runs migrations on startup
# (db.upgrade) this would otherwise replace uvicorn's logging setup.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(get_engine().url.render_as_string(hide_password=False)),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with get_engine().connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite (tests) can't ALTER most things in place
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
