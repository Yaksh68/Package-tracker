"""Phase 4.6: in-memory rate limits in the public mode."""
import pytest
from fastapi.testclient import TestClient

from app import accounts, crypto, ratelimit
from app.config import get_settings
from app.main import app

CSRF = {"X-Requested-With": "packages-tracker"}


@pytest.fixture
def public(monkeypatch):
    for k, v in {"AUTH_MODE": "github", "RATE_LIMIT_SIGNIN": "3", "RATE_LIMIT_SESSION": "5",
                 "RATE_LIMIT_ADDRESS": "8", "RATE_LIMIT_ANONYMOUS": "4",
                 "GITHUB_OAUTH_CLIENT_ID": "cid", "GITHUB_OAUTH_CLIENT_SECRET": "cs"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    crypto.reset()


def _signed_in(login: str, github_id: int) -> TestClient:
    user, _ = accounts.sign_in_github_user({"id": github_id, "login": login}, "gho_x", admin=False)
    c = TestClient(app, headers=CSRF)
    c.cookies.set("pt_session", accounts.create_session(user.id))
    return c


def test_a_session_gets_its_allowance_then_429_with_retry_after(public):
    c = _signed_in("busy", 1)
    assert [c.get("/api/repos").status_code for _ in range(5)] == [200] * 5
    r = c.get("/api/repos")
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 60
    assert "Too many requests" in r.json()["detail"]


def test_made_up_cookies_share_the_addresss_allowance(public):
    statuses = []
    for i in range(10):
        c = TestClient(app, headers=CSRF)
        c.cookies.set("pt_session", f"made-up-{i}")
        statuses.append(c.get("/api/repos").status_code)
    assert statuses[:8] == [401] * 8 and statuses[8:] == [429, 429]


def test_calls_without_a_session_and_sign_ins_have_their_own_limits(public):
    anon = TestClient(app)
    assert [anon.get("/api/me").status_code for _ in range(5)] == [401] * 4 + [429]
    statuses = [anon.get("/api/auth/github/login", follow_redirects=False).status_code for _ in range(4)]
    assert statuses == [302, 302, 302, 429]


def test_health_is_never_limited(public):
    c = TestClient(app)
    assert {c.get("/api/health").status_code for _ in range(20)} == {200}


def test_the_local_mode_has_no_rate_limits(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ANONYMOUS", "1")
    get_settings.cache_clear()
    c = TestClient(app)
    assert {c.get("/api/repos").status_code for _ in range(5)} == {200}


def test_the_window_slides():
    w = ratelimit.SlidingWindow()
    assert [w.hit("k", 2) for _ in range(2)] == [None, None]
    assert w.hit("k", 2) is not None
    w._hits["k"][0] -= ratelimit.WINDOW + 1  # the oldest hit is now out of the window
    assert w.hit("k", 2) is None
