"""Phase 1: run_text_agent routes to the configured provider, enrich() reports
"no_key" when nothing can run the AI, the citation check applies to
provider results, the CLI is found on Windows, and the AI extraction
fallback can't fail a scan."""
import os
from pathlib import Path

import pytest

from app import settings_store
from app.agents import enrichment_agent as ea
from app.agents import extraction_agent, transport
from app.ai.errors import AiConfigError
from app.ai.result import AgentResult
from app.config import get_settings
from app.models import Confidence, Ecosystem, Enrichment, Package, Verdict


def _env(monkeypatch, **env):
    for k in ("AI_PROVIDER", "AI_MODEL", "AI_API_KEY", "AI_BASE_URL", "SEARCH_PROVIDER",
              "SEARCH_API_KEY", "AUTH_MODE", "ANTHROPIC_API_KEY", "CLAUDE_CLI_PATH"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


async def test_the_anthropic_provider_gets_research_calls(monkeypatch):
    _env(monkeypatch, AI_PROVIDER="anthropic", AI_API_KEY="sk-ant-x", WEB_SEARCH_MAX_USES="4")
    seen = {}

    async def research(cfg, prompt, system, *, max_searches, timeout):
        seen.update(model=cfg.model, max_searches=max_searches, system=system)
        return AgentResult(text="{}")

    monkeypatch.setattr(transport.anthropic_provider, "research", research)
    res = await transport.run_text_agent("p", allowed_tools=["WebSearch", "WebFetch"], append_system="sys")
    assert res.text == "{}"
    assert seen == {"model": "claude-haiku-5-5", "max_searches": 4, "system": "sys"}


async def test_openai_compatible_providers_get_research_calls(monkeypatch):
    _env(monkeypatch, AI_PROVIDER="gemini", AI_API_KEY="AIza-x", AI_MODEL="gemini-flash",
         SEARCH_PROVIDER="tavily", SEARCH_API_KEY="tvly-x")
    called = []

    async def research(cfg, prompt, system, *, max_searches, timeout):
        called.append(cfg.label)
        return AgentResult(text="{}")

    monkeypatch.setattr(transport.openai_compat, "research", research)
    await transport.run_text_agent("p", allowed_tools=["WebSearch", "WebFetch"])
    assert called == ["openai-compatible:gemini"]


async def test_api_providers_refuse_the_file_reading_extraction_agent(monkeypatch):
    _env(monkeypatch, AI_PROVIDER="anthropic", AI_API_KEY="sk-ant-x")
    with pytest.raises(AiConfigError, match="needs the Claude CLI"):
        await transport.run_text_agent("p", allowed_tools=["Read", "Glob", "Grep"])


async def test_no_usable_provider_raises_a_config_error(monkeypatch):
    _env(monkeypatch, AUTH_MODE="github")
    with pytest.raises(AiConfigError):
        await transport.run_text_agent("p", allowed_tools=["WebSearch"])


async def test_the_cli_is_still_the_local_default(monkeypatch):
    _env(monkeypatch)
    used = {}

    async def fake_cli(prompt, **kw):
        used.update(kw)
        return AgentResult(text="{}")

    monkeypatch.setattr(transport, "_run_cli", fake_cli)
    await transport.run_text_agent("p", allowed_tools=["WebSearch", "WebFetch"])
    assert used["model"] == get_settings().agent_model


# --------------------------------------------------------------------------- #
# enrich() / _run_ai_batch                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def structured(monkeypatch):
    async def fake_enrich_one(client, pkg, *, today, distro=None, index=None):
        return Enrichment(name=pkg.name, version=pkg.version, ecosystem=pkg.ecosystem,
                          verdict=Verdict.supported, confidence=Confidence.low, summary="structured")
    monkeypatch.setattr(ea, "_enrich_one", fake_enrich_one)


async def test_ai_on_without_a_provider_reports_no_key(monkeypatch, structured):
    _env(monkeypatch, AUTH_MODE="github")
    settings_store.update_runtime_settings({"use_enrichment_agent": True})

    async def never(*a, **kw):
        raise AssertionError("no AI call without a provider")

    monkeypatch.setattr(ea, "run_text_agent", never)
    outcome = ea.AiOutcome()
    out = await ea.enrich([Package(name="lib", version="1", ecosystem=Ecosystem.pypi, source_file="r.txt")],
                          ai_outcome=outcome)
    assert out[0].summary == "structured"
    assert outcome.status == "no_key" and "no AI provider" in outcome.detail


async def test_provider_results_go_through_the_citation_check(monkeypatch):
    seen = {"https://pypi.org/project/cybox/"}
    text = ('{"verdicts": ['
            '{"name": "cybox", "ecosystem": "pypi", "verdict": "deprecated", "successor": "stix2",'
            ' "confidence": "high", "summary": "s", "sources": [{"url": "https://pypi.org/project/cybox", "note": "n"}]},'
            '{"name": "ghost", "ecosystem": "pypi", "verdict": "eol", "successor": null,'
            ' "confidence": "high", "summary": "s", "sources": [{"url": "https://invented.example/", "note": "n"}]}]}')

    async def agent(prompt, **kw):
        return AgentResult(text=text, seen_urls=seen)

    monkeypatch.setattr(ea, "run_text_agent", agent)
    batch = [Enrichment(name=n, version="1", ecosystem=Ecosystem.pypi) for n in ("cybox", "ghost")]
    result, _ = await ea._run_ai_batch(batch, max_searches=2)
    assert [v.name for v in result.verdicts] == ["cybox"]


async def test_typed_provider_errors_keep_their_kind(monkeypatch):
    from app.ai.errors import AiQuotaError

    async def agent(prompt, **kw):
        raise AiQuotaError("groq daily limit reached")

    monkeypatch.setattr(ea, "run_text_agent", agent)
    errors = []
    result, _ = await ea._run_ai_batch([Enrichment(name="x", ecosystem=Ecosystem.pypi)],
                                       max_searches=1, errors=errors)
    assert result is None and errors[0][0] == "quota"


def test_error_text_scrubs_keys():
    text = ea._error_text(RuntimeError("bad key sk-ant-api03-abcdefghijklmnop and tvly-1234567890"))
    assert "sk-ant" not in text and "tvly-" not in text and "***" in text


# --------------------------------------------------------------------------- #
# CLI discovery, extraction fallback                                            #
# --------------------------------------------------------------------------- #
def test_the_newest_extension_cli_is_found_by_version(monkeypatch, tmp_path):
    _env(monkeypatch)
    monkeypatch.setattr(transport.shutil, "which", lambda name: None)
    monkeypatch.setattr(transport.Path, "home", lambda: tmp_path)
    name = "claude.exe" if os.name == "nt" else "claude"
    for version in ("2.1.99", "2.1.314", "2.1.4"):
        d = tmp_path / ".vscode" / "extensions" / f"anthropic.claude-code-{version}-x" / "resources" / "native-binary"
        d.mkdir(parents=True)
        (d / name).write_text("")
    assert "2.1.314" in transport.resolve_cli_path()
    assert Path(transport.resolve_cli_path()).name == name


async def test_an_unavailable_extraction_agent_keeps_the_parsed_packages(monkeypatch, tmp_path):
    async def unavailable(*a, **kw):
        raise AiConfigError("anthropic can only research packages; the AI extraction agent needs the Claude CLI")

    monkeypatch.setattr(extraction_agent, "run_text_agent", unavailable)
    assert await extraction_agent.extract_with_agent(tmp_path, ["requirements.txt"]) == []
