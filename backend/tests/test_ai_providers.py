"""Phase 1 provider layer: config resolution, pricing, the citation check, web
search backends, fetch_url, and both API adapters. Everything here runs
against fakes, with no network calls and no keys."""
from types import SimpleNamespace as NS

import anthropic
import httpx
import openai
import pytest

from app.ai import anthropic_provider, citations, openai_compat, pricing, search
from app.ai import config as ai_config
from app.ai.config import AiConfig
from app.ai.errors import AiAuthError, AiConfigError, AiProviderError, AiQuotaError
from app.ai.search import ExaSearch, FetchRefused, SearchHit, TavilySearch, fetch_text, html_to_text
from app.config import get_settings
from app.git_rules import RepoUrlError
from app.models import AiVerdict, AiVerdictBatch, Confidence, Ecosystem, Source, Verdict


def _env(monkeypatch, **env):
    for k in ("AI_PROVIDER", "AI_MODEL", "AI_API_KEY", "AI_BASE_URL", "SEARCH_PROVIDER",
              "SEARCH_API_KEY", "AUTH_MODE", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# config                                                                        #
# --------------------------------------------------------------------------- #
def test_local_mode_defaults_to_the_claude_cli(monkeypatch):
    _env(monkeypatch)
    cfg, why = ai_config.resolve()
    assert why is None and cfg.provider == "claude-cli" and cfg.model == get_settings().agent_model


def test_the_public_mode_never_uses_the_cli_or_the_servers_env(monkeypatch):
    _env(monkeypatch, AUTH_MODE="github")
    assert ai_config.resolve() == (None, "no AI provider is configured")
    # the server's env is ignored in the public mode: users bring their own keys
    _env(monkeypatch, AUTH_MODE="github", AI_PROVIDER="anthropic", AI_API_KEY="sk-ant-server")
    assert ai_config.resolve() == (None, "no AI provider is configured")
    # a workspace that stored "claude-cli" before the switch still can't use it
    from app import accounts
    from app.models import LOCAL_WORKSPACE_ID
    accounts.set_workspace_settings(LOCAL_WORKSPACE_ID, {"ai_provider": "claude-cli"})
    cfg, why = ai_config.resolve()
    assert cfg is None and "public mode" in why


def test_a_workspace_uses_its_own_settings_and_encrypted_keys(monkeypatch):
    _env(monkeypatch, AUTH_MODE="github")
    from app import accounts
    from app.db import session
    from app.db_models import SecretRow, WorkspaceRow
    import datetime as dt
    with session() as s:
        s.add(WorkspaceRow(id="ws1", name="team", personal=False, created_at=dt.datetime.now(dt.timezone.utc)))
    accounts.set_workspace_settings("ws1", {"ai_provider": "anthropic", "ai_model": "claude-sonnet-5-5"})
    accounts.set_secret("ws1", "ai_api_key", "sk-ant-workspace-key-123")
    cfg, why = ai_config.resolve(workspace_id="ws1")
    assert why is None and (cfg.provider, cfg.model, cfg.api_key) == (
        "anthropic", "claude-sonnet-5-5", "sk-ant-workspace-key-123")
    with session() as s:  # stored encrypted, never in plain text
        assert s.get(SecretRow, ("ws1", "ai_api_key")).ciphertext.startswith("enc:v1:")


def test_anthropic_needs_a_key_and_defaults_to_haiku_5_5(monkeypatch):
    _env(monkeypatch, AI_PROVIDER="anthropic")
    cfg, why = ai_config.resolve()
    assert cfg is None and "API key" in why
    _env(monkeypatch, AI_PROVIDER="anthropic", AI_API_KEY="sk-ant-test")
    cfg, _ = ai_config.resolve()
    assert (cfg.provider, cfg.model, cfg.label) == ("anthropic", "claude-haiku-5-5", "anthropic")


def test_openai_compatible_presets_need_a_model_and_a_search_key(monkeypatch):
    _env(monkeypatch, AI_PROVIDER="groq", AI_API_KEY="gsk_x")
    assert "choose a model" in ai_config.resolve()[1]
    _env(monkeypatch, AI_PROVIDER="groq", AI_API_KEY="gsk_x", AI_MODEL="openai/gpt-oss-120b")
    assert "SEARCH_PROVIDER" in ai_config.resolve()[1]
    _env(monkeypatch, AI_PROVIDER="groq", AI_API_KEY="gsk_x", AI_MODEL="openai/gpt-oss-120b",
         SEARCH_PROVIDER="tavily", SEARCH_API_KEY="tvly-x")
    cfg, why = ai_config.resolve()
    assert why is None
    assert cfg.base_url == "https://api.groq.com/openai/v1"
    assert cfg.label == "openai-compatible:groq"


def test_a_custom_endpoint_needs_a_base_url_but_not_a_key(monkeypatch):
    _env(monkeypatch, AI_PROVIDER="openai-compatible", AI_MODEL="llama3")
    assert "base URL" in ai_config.resolve()[1]
    _env(monkeypatch, AI_PROVIDER="openai-compatible", AI_MODEL="llama3",
         AI_BASE_URL="http://localhost:11434/v1", SEARCH_PROVIDER="exa", SEARCH_API_KEY="k")
    cfg, why = ai_config.resolve()
    assert why is None and cfg.api_key is None and cfg.label == "openai-compatible:custom"


def test_an_unknown_provider_is_reported(monkeypatch):
    _env(monkeypatch, AI_PROVIDER="skynet")
    assert ai_config.resolve() == (None, "unknown AI_PROVIDER 'skynet'")


# --------------------------------------------------------------------------- #
# pricing                                                                       #
# --------------------------------------------------------------------------- #
def test_claude_cost_includes_cache_and_search_and_matches_the_longest_prefix():
    usage = {"input_tokens": 10_000, "output_tokens": 2_000, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "server_tool_use": {"web_search_requests": 3}}
    # haiku 5.5: 10k * 0.10/M + 2k * 0.50/M + 3 searches * $0.01
    assert pricing.claude_cost("claude-haiku-5-5", usage) == pytest.approx(0.001 + 0.001 + 0.03)
    # opus 5.5 is not priced as opus 5
    assert pricing.claude_cost("claude-opus-5-5", usage) == pytest.approx(0.04 + 0.04 + 0.03)
    # dated IDs match their family
    assert pricing.claude_cost("claude-haiku-4-5-20251001", usage) == pytest.approx(0.01 + 0.01 + 0.03)
    assert pricing.claude_cost("some-other-model", usage) is None


def test_haiku_5_5_long_prompts_use_the_second_rate_card():
    usage = {"input_tokens": 150_000, "output_tokens": 0}
    assert pricing.claude_cost("claude-haiku-5-5", usage) == pytest.approx(150_000 * 0.50 / 1e6)


def test_third_party_models_are_free_or_not_estimated():
    assert pricing.openai_compatible_cost("meta-llama/llama-3.3-70b:free") == 0.0
    assert pricing.openai_compatible_cost("gpt-5") is None


# --------------------------------------------------------------------------- #
# citation check                                                                #
# --------------------------------------------------------------------------- #
def _verdict(name, verdict, *urls, successor=None):
    return AiVerdict(name=name, ecosystem=Ecosystem.pypi, verdict=verdict, successor=successor,
                     confidence=Confidence.medium, summary="s",
                     sources=[Source(url=u, note="n") for u in urls])


def test_citations_the_research_never_saw_are_dropped():
    seen = {"https://github.com/CybOXProject/python-cybox/", "http://pypi.org/project/stix2"}
    batch = AiVerdictBatch(verdicts=[
        _verdict("cybox", Verdict.deprecated, "https://github.com/cyboxproject/python-cybox#readme",
                 "https://made-up.example/cybox", successor="stix2"),
        _verdict("ghost", Verdict.eol, "https://made-up.example/ghost"),
        _verdict("fine", Verdict.supported, successor="newfine"),
    ])
    out, report = citations.enforce(batch, seen)
    by_name = {v.name: v for v in out.verdicts}

    assert [s.url for s in by_name["cybox"].sources] == [
        "https://github.com/cyboxproject/python-cybox#readme"]  # normalized match
    assert by_name["cybox"].successor == "stix2"
    assert "ghost" not in by_name  # an unbacked "eol" keeps the structured verdict
    assert by_name["fine"].verdict == Verdict.supported and by_name["fine"].successor is None
    assert report == {"citations": 2, "verdicts": ["ghost"], "successors": ["fine"]}


def test_without_seen_urls_only_uncited_claims_are_dropped():
    batch = AiVerdictBatch(verdicts=[
        _verdict("nose", Verdict.deprecated, successor="pytest"),           # no citation at all
        _verdict("cybox", Verdict.deprecated, "https://anything.example/"),  # cited: kept as is
        _verdict("react", Verdict.supported),                                # supported needs none
    ])
    out, report = citations.enforce(batch, None)
    assert [v.name for v in out.verdicts] == ["cybox", "react"]
    assert report["verdicts"] == ["nose"]


def test_url_normalization():
    assert citations.normalize("HTTP://Example.org/a/#x") == citations.normalize("https://example.org/a")


# --------------------------------------------------------------------------- #
# search backends                                                               #
# --------------------------------------------------------------------------- #
def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_tavily_sends_a_bearer_key_and_parses_results():
    def handler(request):
        assert request.url == "https://api.tavily.com/search"
        assert request.headers["authorization"] == "Bearer tvly-test"
        assert b'"max_results":5' in request.content.replace(b" ", b"")
        return httpx.Response(200, json={"results": [
            {"url": "https://a.example/x", "title": "A", "content": "about x"},
            {"title": "no url, skipped"},
        ]})

    async with _client(handler) as c:
        hits = await TavilySearch("tvly-test", c).search("x")
    assert hits == [SearchHit(url="https://a.example/x", title="A", snippet="about x")]


async def test_exa_sends_x_api_key_and_joins_highlights():
    def handler(request):
        assert request.url == "https://api.exa.ai/search"
        assert request.headers["x-api-key"] == "exa-key"
        return httpx.Response(200, json={"results": [
            {"url": "https://b.example/y", "title": "B", "highlights": ["one", "two"]}]})

    async with _client(handler) as c:
        hits = await ExaSearch("exa-key", c).search("y")
    assert hits[0].snippet == "one … two"


@pytest.mark.parametrize("status,body,error", [
    (401, "", AiAuthError), (429, "", AiQuotaError), (432, "plan limit exceeded", AiQuotaError),
    (500, "boom", AiProviderError),
])
async def test_search_errors_are_typed(status, body, error):
    async with _client(lambda r: httpx.Response(status, text=body)) as c:
        with pytest.raises(error):
            await TavilySearch("k", c).search("q")


# --------------------------------------------------------------------------- #
# fetch_url                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def public_dns(monkeypatch):
    def check(host):
        if host.startswith("internal"):
            raise RepoUrlError(f"{host} resolves to a non-public address (10.0.0.5), which isn't allowed")
    monkeypatch.setattr(search, "require_public_address", check)


async def test_fetch_returns_page_text_without_scripts(public_dns):
    html = "<html><head><style>x{}</style></head><body><script>evil()</script><p>Deprecated, use stix2.</p></body></html>"
    async with _client(lambda r: httpx.Response(200, html=html)) as c:
        url, text = await fetch_text("https://docs.example/page", c)
    assert url == "https://docs.example/page" and text == "Deprecated, use stix2."


async def test_fetch_rechecks_every_redirect_hop(public_dns):
    def handler(request):
        return httpx.Response(302, headers={"location": "https://internal.corp/secret"})

    async with _client(handler) as c:
        with pytest.raises(FetchRefused, match="non-public"):
            await fetch_text("https://docs.example/redirect", c)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.example/a", "https://10.0.0.1/a",
                                 "https://user:pw@docs.example/a"])
async def test_fetch_refuses_non_public_or_odd_urls(public_dns, url):
    async with _client(lambda r: httpx.Response(200, text="x")) as c:
        with pytest.raises(FetchRefused):
            await fetch_text(url, c)


async def test_fetch_refuses_binary_content(public_dns):
    async with _client(lambda r: httpx.Response(200, content=b"\x00\x01",
                                                headers={"content-type": "application/zip"})) as c:
        with pytest.raises(FetchRefused, match="not a text page"):
            await fetch_text("https://docs.example/file.zip", c)


def test_html_to_text_collapses_whitespace():
    assert html_to_text("<div>a\n\n  <b>b</b></div>") == "a b"


# --------------------------------------------------------------------------- #
# Anthropic adapter                                                             #
# --------------------------------------------------------------------------- #
class _Stream:
    def __init__(self, msg):
        self.msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        if isinstance(self.msg, Exception):
            raise self.msg
        return self.msg


def _fake_anthropic(monkeypatch, replies):
    calls = []

    class Msgs:
        def __init__(self, kind):
            self.kind = kind

        def stream(self, **kw):
            calls.append((self.kind, kw))
            return _Stream(replies.pop(0))

    class Client:
        def __init__(self, **kw):
            self.messages = Msgs("messages")
            self.beta = NS(messages=Msgs("beta"))

        async def close(self):
            pass

    monkeypatch.setattr(anthropic_provider.anthropic, "AsyncAnthropic", Client)
    return calls


def _usage(i=100, o=20, searches=1):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches, web_fetch_requests=0))


_JSON = '{"verdicts": []}'


async def test_anthropic_resumes_a_paused_turn_and_collects_what_search_saw(monkeypatch):
    paused = NS(stop_reason="pause_turn", usage=_usage(), content=[
        NS(type="server_tool_use", name="web_search", input={"query": "cybox deprecated"}),
        NS(type="web_search_tool_result", content=[
            NS(type="web_search_result", url="https://github.com/CybOXProject/python-cybox", title="cybox")]),
    ])
    final = NS(stop_reason="end_turn", usage=_usage(i=50, o=30, searches=0), content=[
        NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url="https://oasis-open.github.io/cti-documentation/")),
        NS(type="text", text=_JSON),
    ])
    calls = _fake_anthropic(monkeypatch, [paused, final])
    cfg = AiConfig(provider="anthropic", model="claude-haiku-5-5", api_key="sk-ant-x")
    res = await anthropic_provider.research(cfg, "prompt", "system", max_searches=5, timeout=30)

    assert res.text == _JSON
    assert len(calls) == 2 and calls[0][0] == "messages"
    assert calls[1][1]["messages"][-1] == {"role": "assistant", "content": paused.content}
    assert [t["type"] for t in calls[0][1]["tools"]] == ["web_search_20250305", "web_fetch_20250910"]
    assert res.seen_urls == {"https://github.com/CybOXProject/python-cybox",
                             "https://oasis-open.github.io/cti-documentation/"}
    tokens = res.tokens()
    assert (tokens["input_tokens"], tokens["output_tokens"], tokens["web_search_requests"]) == (150, 50, 1)
    assert tokens["cost_usd"] == pytest.approx(150 * 0.10 / 1e6 + 50 * 0.50 / 1e6 + 0.01)


async def test_newer_models_get_dynamic_filtering_tools_and_refusal_fallbacks(monkeypatch):
    calls = _fake_anthropic(monkeypatch, [NS(stop_reason="end_turn", usage=_usage(),
                                             content=[NS(type="text", text=_JSON)])])
    cfg = AiConfig(provider="anthropic", model="claude-sonnet-5-5", api_key="sk-ant-x")
    await anthropic_provider.research(cfg, "p", "s", max_searches=3, timeout=30)
    kind, kw = calls[0]
    assert kind == "beta"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert [t["type"] for t in kw["tools"]] == ["web_search_20260209", "web_fetch_20260209"]
    assert kw["tools"][0]["max_uses"] == 3


async def test_a_refusal_fails_the_batch(monkeypatch):
    _fake_anthropic(monkeypatch, [NS(stop_reason="refusal", usage=_usage(), content=[])])
    cfg = AiConfig(provider="anthropic", model="claude-haiku-5-5", api_key="k")
    with pytest.raises(AiProviderError, match="refusal"):
        await anthropic_provider.research(cfg, "p", "s", max_searches=1, timeout=30)


def _http_error(cls, status, message):
    req = httpx.Request("POST", "https://api.example/v1/messages")
    return cls(message, response=httpx.Response(status, request=req), body=None)


@pytest.mark.parametrize("error,expected", [
    (_http_error(anthropic.RateLimitError, 429, "slow down"), AiQuotaError),
    (_http_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), AiAuthError),
    (_http_error(anthropic.BadRequestError, 400, "Your credit balance is too low"), AiQuotaError),
    (_http_error(anthropic.NotFoundError, 404, "model not found"), AiConfigError),
])
async def test_anthropic_sdk_errors_become_typed_errors(monkeypatch, error, expected):
    _fake_anthropic(monkeypatch, [error])
    cfg = AiConfig(provider="anthropic", model="claude-haiku-5-5", api_key="k")
    with pytest.raises(expected):
        await anthropic_provider.research(cfg, "p", "s", max_searches=1, timeout=30)


# --------------------------------------------------------------------------- #
# OpenAI-compatible adapter                                                     #
# --------------------------------------------------------------------------- #
def _call(cid, name, args):
    return NS(id=cid, function=NS(name=name, arguments=args))


def _reply(content=None, calls=None, p=100, c=10):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))],
              usage=NS(prompt_tokens=p, completion_tokens=c))


@pytest.fixture
def compat(monkeypatch):
    """Fake OpenAI client, search backend and fetcher; returns (setup, state)."""
    state = NS(calls=[], searches=[], fetches=[])

    class FakeSearch:
        async def search(self, query, *, limit=5):
            state.searches.append(query)
            return [SearchHit(url="https://pypi.org/project/cybox/", title="cybox", snippet="…")]

    async def fake_fetch(url, http):
        state.fetches.append(url)
        return url, "The cybox package is deprecated; use stix2."

    monkeypatch.setattr(openai_compat, "make_search", lambda *a: FakeSearch())
    monkeypatch.setattr(openai_compat, "fetch_text", fake_fetch)

    def setup(replies):
        class Completions:
            async def create(self, **kw):
                state.calls.append(kw)
                return replies.pop(0)

        class Client:
            def __init__(self, **kw):
                self.chat = NS(completions=Completions())

            async def close(self):
                pass

        monkeypatch.setattr(openai_compat.openai, "AsyncOpenAI", Client)

    return setup, state


def _cfg(model="openai/gpt-oss-120b", rounds=8):
    return AiConfig(provider="openai-compatible", model=model, preset="groq", api_key="gsk_x",
                    base_url="https://api.groq.com/openai/v1", search_provider="tavily",
                    search_api_key="tvly-x", max_tool_rounds=rounds)


async def test_the_tool_loop_searches_fetches_and_answers(compat):
    setup, state = compat
    setup([
        _reply(calls=[_call("c1", "web_search", '{"query": "cybox deprecated"}')]),
        _reply(calls=[_call("c2", "fetch_url", '{"url": "https://pypi.org/project/cybox"}')]),
        _reply(content=_JSON),
    ])
    res = await openai_compat.research(_cfg(), "prompt", "system", max_searches=5, timeout=30)

    assert res.text == _JSON
    assert state.searches == ["cybox deprecated"]
    assert state.fetches == ["https://pypi.org/project/cybox"]
    assert "https://pypi.org/project/cybox/" in res.seen_urls
    tokens = res.tokens()
    assert (tokens["input_tokens"], tokens["web_search_requests"], tokens["web_fetch_requests"]) == (300, 1, 1)
    assert tokens["cost_usd"] is None  # not a free model: not estimated
    tool_msgs = [m for m in state.calls[-1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]


async def test_fetch_only_accepts_urls_the_search_returned(compat):
    setup, state = compat
    setup([
        _reply(calls=[_call("c1", "fetch_url", '{"url": "http://169.254.169.254/latest/meta-data"}')]),
        _reply(content=_JSON),
    ])
    await openai_compat.research(_cfg(), "p", "s", max_searches=5, timeout=30)
    assert state.fetches == []
    tool_msg = next(m for m in state.calls[-1]["messages"] if m["role"] == "tool")
    assert "only URLs returned by web_search" in tool_msg["content"]


async def test_the_search_limit_is_enforced(compat):
    setup, state = compat
    setup([
        _reply(calls=[_call("c1", "web_search", '{"query": "a"}'), _call("c2", "web_search", '{"query": "b"}')]),
        _reply(content=_JSON),
    ])
    res = await openai_compat.research(_cfg(), "p", "s", max_searches=1, timeout=30)
    assert state.searches == ["a"] and res.tokens()["web_search_requests"] == 1


async def test_running_out_of_rounds_asks_for_the_answer(compat):
    setup, state = compat
    setup([
        _reply(calls=[_call("c1", "web_search", '{"query": "a"}')]),
        _reply(content=_JSON),
    ])
    res = await openai_compat.research(_cfg(rounds=1), "p", "s", max_searches=5, timeout=30)
    assert [c["tool_choice"] for c in state.calls] == ["auto", "none"]
    assert res.text == _JSON


async def test_free_models_cost_nothing(compat):
    setup, _ = compat
    setup([_reply(content=_JSON)])
    res = await openai_compat.research(_cfg(model="meta-llama/llama-3.3-70b:free"), "p", "s",
                                       max_searches=1, timeout=30)
    assert res.tokens()["cost_usd"] == 0.0


async def test_openai_sdk_errors_become_typed_errors(compat, monkeypatch):
    class Client:
        def __init__(self, **kw):
            async def create(**kw):
                raise _http_error(openai.RateLimitError, 429, "daily limit reached")
            self.chat = NS(completions=NS(create=create))

        async def close(self):
            pass

    monkeypatch.setattr(openai_compat.openai, "AsyncOpenAI", Client)
    with pytest.raises(AiQuotaError):
        await openai_compat.research(_cfg(), "p", "s", max_searches=1, timeout=30)
