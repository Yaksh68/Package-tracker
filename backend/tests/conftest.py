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
    # encryption at rest needs a key; never let a test generate data/secret.key
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-for-the-test-suite")
    monkeypatch.setenv("LOCAL_SECRET_KEY_FILE", f"{tmp}/secret.key")

    from app import crypto, db, ratelimit
    from app.config import get_settings
    from app.db_models import Base

    get_settings.cache_clear()
    crypto.reset()
    # every TestClient comes from the same address: don't let one test's
    # requests count against the next one's rate limits
    ratelimit.reset()
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
    crypto.reset()
