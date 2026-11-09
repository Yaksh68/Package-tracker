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

    from app import analytics, crypto, db, ratelimit, trial
    from app.config import get_settings
    from app.db_models import Base

    get_settings.cache_clear()
    crypto.reset()
    # every TestClient comes from the same address: don't let one test's
    # requests count against the next one's rate limits
    ratelimit.reset()
    trial.reset()
    analytics.clear_cache()
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


@pytest.fixture
def fake_github(monkeypatch):
    """Fake GitHub's OAuth endpoints for the sign-in routes: after
    `fake_github(profile={...})`, swapping the code answers with `token` and
    /user with `profile`. (A fixture, so any test module can use it however
    pytest is started.)"""
    from urllib.parse import parse_qs

    import httpx

    from app import auth

    def install(*, profile: dict, token: str = "gho_from_github") -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/login/oauth/access_token":
                body = parse_qs(request.content.decode())
                assert body["client_secret"] == ["csecret"] and body["code"] == ["the-code"]
                return httpx.Response(200, json={"access_token": token, "token_type": "bearer"})
            if request.url.path == "/user":
                assert request.headers["authorization"] == f"Bearer {token}"
                return httpx.Response(200, json=profile)
            return httpx.Response(404)

        monkeypatch.setattr(auth, "_github_client",
                            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    return install


@pytest.fixture
def oauth_sign_in():
    """`oauth_sign_in(client, next_page=None)`: run login → callback (with
    fake_github in place); returns the callback's response."""
    from urllib.parse import parse_qs, urlsplit

    def run(c, next_page: str | None = None):
        login = c.get("/api/auth/github/login", params={"next": next_page} if next_page else None,
                      follow_redirects=False)
        assert login.status_code == 302
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        return c.get(f"/api/auth/github/callback?code=the-code&state={state}", follow_redirects=False)

    return run
