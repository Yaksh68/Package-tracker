"""Phase 3 backend: the Settings page's "Test connection", the rules for a
custom AI base URL, the public-only HTTP transport behind it, and each scan's
AI usage in the scan log. No network calls: DNS and HTTP are faked."""
from datetime import datetime, timezone

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app import accounts, crypto, safe_http, settings_store, storage
from app.ai import openai_compat, probe
from app.ai.config import AiConfig
from app.ai.errors import AiAuthError, AiConfigError
from app.config import get_settings
from app.git_rules import RepoUrlError
from app.main import app
from app.models import Repo, ScanLog, ScanRunRecord

CSRF = {"X-Requested-With": "packages-tracker"}


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "github")
    get_settings.cache_clear()
    crypto.reset()


def _dns(monkeypatch, module, table: dict[str, str]):
    """Fake DNS for `module`: hosts in `table` resolve there, the rest fail."""
    def resolve(host):
        ip = table.get(host)
        if ip is None:
            raise RepoUrlError(f"could not resolve {host}")
        if ip.startswith(("10.", "127.", "169.254.")):
            raise RepoUrlError(f"{host} resolves to a non-public address ({ip}), which isn't allowed")
        return [ip]
    monkeypatch.setattr(module, "public_addresses", resolve)


# --------------------------------------------------------------------------- #
# the custom base URL                                                           #
# --------------------------------------------------------------------------- #
def test_the_local_mode_takes_any_http_base_url():
    settings_store.check_ai_base_url("http://localhost:11434/v1")  # a local Ollama


@pytest.mark.parametrize("url,why", [
    ("http://llm.example/v1", "https://"),
    ("https://10.0.0.5/v1", "hostname, not an IP"),
    ("https://user:pw@llm.example/v1", "API key field"),
    ("https://metadata.internal/v1", "non-public"),
    ("https://nowhere.example/v1", "could not resolve"),
    ("ftp://llm.example/v1", "https://"),
])
def test_the_public_mode_refuses_base_urls_that_could_reach_inside(public, monkeypatch, url, why):
    _dns(monkeypatch, settings_store, {"llm.example": "93.184.215.14", "metadata.internal": "169.254.169.254"})
    with pytest.raises(ValueError, match=why):
        settings_store.check_ai_base_url(url)


def test_the_public_mode_accepts_a_public_https_base_url(public, monkeypatch):
    _dns(monkeypatch, settings_store, {"llm.example": "93.184.215.14"})
    settings_store.check_ai_base_url("https://llm.example/v1")


# --------------------------------------------------------------------------- #
# the public-only transport                                                     #
# --------------------------------------------------------------------------- #
async def test_the_transport_connects_to_the_address_it_checked(monkeypatch):
    _dns(monkeypatch, safe_http, {"llm.example": "93.184.215.14"})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = safe_http.PublicOnlyTransport()
    transport._inner = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as c:
        r = await c.get("https://llm.example/v1/models")
    assert r.status_code == 200
    sent = seen[0]
    assert sent.url.host == "93.184.215.14"           # the checked address, not a fresh lookup
    assert sent.headers["host"] == "llm.example"      # the server still sees its own name
    assert sent.extensions["sni_hostname"] == "llm.example"  # and TLS checks its certificate


async def test_the_transport_refuses_internal_addresses(monkeypatch):
    _dns(monkeypatch, safe_http, {"rebind.example": "127.0.0.1"})
    async with safe_http.public_client() as c:
        with pytest.raises(safe_http.AddressRefused, match="non-public"):
            await c.get("https://rebind.example/v1/models")


def test_the_public_client_neither_follows_redirects_nor_uses_env_proxies():
    c = safe_http.public_client()
    assert c.follow_redirects is False and c.trust_env is False


async def test_a_custom_base_url_in_the_public_mode_goes_through_the_check(public, monkeypatch):
    _dns(monkeypatch, safe_http, {"llm.example": "10.0.0.7"})  # the DNS answer changed after saving
    cfg = AiConfig(provider="openai-compatible", model="", base_url="https://llm.example/v1")
    with pytest.raises(AiConfigError, match="base URL can't be used.*non-public"):
        await openai_compat.list_models(cfg)


# --------------------------------------------------------------------------- #
# listing models                                                                #
# --------------------------------------------------------------------------- #
def _fake_endpoint(monkeypatch, models: list[dict], *, key_ok: bool = True):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if not key_ok:
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"label": "k"}})
        return httpx.Response(200, json={"object": "list", "data": models})

    def client(cfg, timeout):
        return openai.AsyncOpenAI(api_key=cfg.api_key or "x", base_url=cfg.base_url, max_retries=0,
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(openai_compat, "_client", client)
    return calls


def _m(mid, params=None):
    out = {"id": mid, "object": "model", "created": 0, "owned_by": "x"}
    if params is not None:
        out["supported_parameters"] = params
    return out


async def test_openrouter_checks_the_key_and_lists_free_tool_models_first(monkeypatch):
    calls = _fake_endpoint(monkeypatch, [
        _m("z-ai/glm-4.6", ["tools"]), _m("meta-llama/llama-3.3-70b-instruct:free", ["tools"]),
        _m("some/no-tools:free", ["temperature"]), _m("openai/text-embedding-3-small", ["tools"]),
    ])
    models = await probe.test_connection("openrouter", api_key="sk-or-v1-x", base_url=None)
    assert models == ["meta-llama/llama-3.3-70b-instruct:free", "z-ai/glm-4.6"]
    assert calls[0].endswith("/key")


async def test_listings_without_tool_info_keep_chat_models(monkeypatch):
    _fake_endpoint(monkeypatch, [_m("gpt-5-mini"), _m("text-embedding-3-large"), _m("whisper-1"),
                                 _m("gpt-4o-mini-tts"), _m("gpt-5")])
    assert await probe.test_connection("openai", api_key="sk-proj-x", base_url=None) == ["gpt-5", "gpt-5-mini"]


async def test_a_rejected_key_is_an_auth_error(monkeypatch):
    _fake_endpoint(monkeypatch, [], key_ok=False)
    with pytest.raises(AiAuthError, match="rejected the API key"):
        await probe.test_connection("groq", api_key="gsk_bad", base_url=None)


@pytest.mark.parametrize("provider,key,base_url,why", [
    ("anthropic", None, None, "Anthropic API key"),
    ("gemini", None, None, "gemini API key"),
    ("openai-compatible", "k", None, "base URL"),
    ("skynet", "k", None, "unknown AI provider"),
])
async def test_missing_pieces_are_named(provider, key, base_url, why):
    with pytest.raises(AiConfigError, match=why):
        await probe.test_connection(provider, api_key=key, base_url=base_url)


async def test_the_cli_is_refused_in_the_public_mode(public):
    with pytest.raises(AiConfigError, match="public mode"):
        await probe.test_connection("claude-cli", api_key=None, base_url=None)


# --------------------------------------------------------------------------- #
# the route                                                                     #
# --------------------------------------------------------------------------- #
def test_the_route_uses_the_typed_key_or_else_the_saved_one(monkeypatch):
    seen = []

    async def fake(provider, *, api_key, base_url):
        seen.append((provider, api_key, base_url))
        return ["m1", "m2"]
    monkeypatch.setattr(probe, "test_connection", fake)
    accounts.set_secret("local", "ai_api_key", "gsk_saved_key_123456")
    c = TestClient(app, headers=CSRF)

    r = c.post("/api/settings/ai-test", json={"provider": "groq"})
    assert r.json() == {"ok": True, "models": ["m1", "m2"]}
    c.post("/api/settings/ai-test", json={"provider": "groq", "api_key": "gsk_typed_key_654321"})
    # a base URL only matters for a custom endpoint
    c.post("/api/settings/ai-test", json={"provider": "groq", "base_url": "https://x.example"})
    assert seen == [("groq", "gsk_saved_key_123456", None), ("groq", "gsk_typed_key_654321", None),
                    ("groq", "gsk_saved_key_123456", None)]


def test_the_route_reports_provider_errors_without_keys(monkeypatch):
    async def fake(provider, *, api_key, base_url):
        raise AiAuthError("groq rejected the API key: Invalid API Key gsk_abcdefghijklmnop")
    monkeypatch.setattr(probe, "test_connection", fake)
    r = TestClient(app, headers=CSRF).post("/api/settings/ai-test", json={"provider": "groq"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and body["kind"] == "auth"
    assert "gsk_abcdefghijklmnop" not in body["message"]


def test_the_route_refuses_a_bad_custom_url_before_calling_anything(public, monkeypatch):
    user, _ = accounts.sign_in_github_user({"id": 4242, "login": "tester"}, "gho_t", admin=False)
    c = TestClient(app, headers=CSRF)
    c.cookies.set("pt_session", accounts.create_session(user.id))
    r = c.post("/api/settings/ai-test", json={"provider": "openai-compatible",
                                               "base_url": "http://plain.example/v1"})
    assert r.status_code == 422 and "https://" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# AI usage in the scan log                                                      #
# --------------------------------------------------------------------------- #
def test_the_scan_log_carries_that_scans_ai_usage():
    ts = "2026-11-08T10-00-00Z"
    storage.upsert_repo(Repo(id="usage", url="https://x/usage", branches=["main"]))
    storage.write_scan_log("usage", "main", ts, ScanLog(repo_id="usage", branch="main", snapshot_ts=ts))
    storage.write_scan_run(ScanRunRecord(
        repo_id="usage", branch="main", snapshot_ts=ts, started_at=datetime.now(timezone.utc),
        outcome="done", stage_seconds={"ai": 12.5}, ai_status="ok", ai_provider="anthropic",
        ai_model="claude-haiku-5-5", ai_batches=2, ai_tokens_in=5000, ai_tokens_out=800,
        ai_searches=3, ai_cost_usd=0.0335))
    c = TestClient(app)
    usage = c.get(f"/api/repos/usage/scan-log?branch=main&ts={ts}").json()["ai_usage"]
    assert usage["provider"] == "anthropic" and usage["searches"] == 3 and usage["seconds"] == 12.5
    assert usage["tokens_in"] == 5000 and usage["cost_usd"] == pytest.approx(0.0335)
    # a scan from before scan_runs existed has none
    old = "2026-01-01T00-00-00Z"
    storage.write_scan_log("usage", "main", old, ScanLog(repo_id="usage", branch="main", snapshot_ts=old))
    assert c.get(f"/api/repos/usage/scan-log?branch=main&ts={old}").json()["ai_usage"] is None
