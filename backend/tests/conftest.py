import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """Every test gets a throwaway SQLite database (schema from the ORM models;
    `test_db_storage` separately checks the Alembic migrations match them) and
    a throwaway legacy data dir. Set TEST_DATABASE_URL to run against Postgres
    instead — tables are then truncated between tests."""
    tmp = tempfile.mkdtemp(prefix="packages-tracker-test-")
    monkeypatch.setenv("EOL_DATA_DIR", tmp)
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp}/test.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("UPLOAD_DIR", f"{tmp}/uploads")
    monkeypatch.setenv("APT_INDEX_CACHE_DIR", f"{tmp}/apt-index")

    from app import db
    from app.config import get_settings
    from app.db_models import Base

    get_settings.cache_clear()
    db.reset_engine()
    engine = db.get_engine()
    if url.startswith("sqlite"):
        Base.metadata.create_all(engine)
    else:
        db.upgrade()
        with engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
    yield tmp
    db.reset_engine()
    get_settings.cache_clear()
