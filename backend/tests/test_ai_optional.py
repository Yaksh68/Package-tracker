"""AI research is optional (phase 0, task 0.3).

Whatever the AI does, including raising, every structured verdict survives,
the snapshot isn't marked degraded, and the outcome is reported on its own.
Before this, an exception from the AI escaped enrich(): the pipeline then
carried on with an empty enrichment list and flagged every package UNKNOWN.
"""
import datetime as dt

import pytest

from app import pipeline, settings_store, storage
from app.agents import enrichment_agent as ea
from app.agents.clone import Clone
from app.agents.transport import AgentError
from app.config import get_settings
from app.models import Confidence, Ecosystem, Enrichment, Package, PackageList, Repo, Verdict

TODAY = dt.date(2026, 7, 14)


def _tokens():
    return dict(input_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                output_tokens=10, total_tokens=110, web_search_requests=1,
                web_fetch_requests=0, cost_usd=0.001, num_turns=2)


class _Res:
    """Stands in for transport.AgentResult."""

    def __init__(self, text="", *, is_error=False, error_message=""):
        self.text, self.is_error, self.error_message = text, is_error, error_message

    def tokens(self):
        return _tokens()


def _verdicts_for(*names):
    items = ",".join(
        f'{{"name":"{n}","ecosystem":"pypi","verdict":"deprecated","successor":null,'
        f'"confidence":"medium","summary":"AI says deprecated",'
        f'"sources":[{{"url":"https://example.org/{n}","note":"x"}}]}}'
        for n in names
    )
    return _Res(f'{{"verdicts":[{items}]}}')


def _pkgs(n):
    return [Package(name=f"lib{i}", version="1.0", ecosystem=Ecosystem.pypi,
                    source_file="requirements.txt") for i in range(n)]


@pytest.fixture
def structured(monkeypatch):
    """Structured lookups without the network: every package comes back
    `supported` with low confidence, which is ambiguous enough to send it to
    the AI pass."""
    async def fake_enrich_one(client, pkg, *, today, distro=None, index=None):
        return Enrichment(name=pkg.name, version=pkg.version, ecosystem=pkg.ecosystem,
                          verdict=Verdict.supported, confidence=Confidence.low,
                          summary="structured")
    monkeypatch.setattr(ea, "_enrich_one", fake_enrich_one)


@pytest.fixture
def ai_on(monkeypatch):
    settings_store.update_runtime_settings({"use_enrichment_agent": True})
    monkeypatch.setenv("ENRICHMENT_BATCH_SIZE", "1")  # one batch per package
    get_settings.cache_clear()


def _agent(monkeypatch, behaviour):
    """Install a fake run_text_agent; `behaviour(call_number)` returns a
    result or raises. Returns the list of call numbers seen."""
    calls: list[int] = []

    async def fake(prompt, **kw):
        calls.append(len(calls) + 1)
        return behaviour(len(calls))

    monkeypatch.setattr(ea, "run_text_agent", fake)
    return calls


async def test_an_ai_exception_keeps_every_structured_verdict(monkeypatch, structured, ai_on):
    def timeout(n):
        raise AgentError("agent timed out after 420.0s")

    calls = _agent(monkeypatch, timeout)
    outcome = ea.AiOutcome()
    out = await ea.enrich(_pkgs(3), today=TODAY, ai_outcome=outcome)

    assert [e.summary for e in out] == ["structured"] * 3
    assert all(e.verdict == Verdict.supported and not e.ai_checked for e in out)
    assert len(calls) == 3  # a timeout is per batch: the others still run, without retries
    assert outcome.status == "failed"
    assert outcome.failed_batches == 3
    assert "timed out" in outcome.detail


async def test_a_rejected_key_stops_the_remaining_batches(monkeypatch, structured, ai_on):
    def rejected(n):
        raise AgentError("claude CLI exited 1: 401 authentication_error: invalid x-api-key")

    calls = _agent(monkeypatch, rejected)
    outcome = ea.AiOutcome()
    out = await ea.enrich(_pkgs(3), today=TODAY, ai_outcome=outcome)

    assert len(out) == 3
    assert calls == [1]  # the other two batches would fail the same way
    assert outcome.status == "failed"
    assert outcome.skipped_batches == 2
    assert "authentication" in outcome.detail


async def test_an_exhausted_quota_is_reported_as_quota(monkeypatch, structured, ai_on):
    calls = _agent(monkeypatch, lambda n: _Res(is_error=True,
                                               error_message="429 rate_limit_error: slow down"))
    outcome = ea.AiOutcome()
    await ea.enrich(_pkgs(2), today=TODAY, ai_outcome=outcome)

    assert calls == [1]  # no retry, no second batch
    assert outcome.status == "quota"


async def test_some_failed_batches_make_a_partial_result(monkeypatch, structured, ai_on):
    def flaky(n):
        if n == 2:
            raise AgentError("agent timed out after 420.0s")
        return _verdicts_for("lib0", "lib1", "lib2")

    _agent(monkeypatch, flaky)
    outcome = ea.AiOutcome()
    out = {e.name: e for e in await ea.enrich(_pkgs(3), today=TODAY, ai_outcome=outcome)}

    assert outcome.status == "partial"
    assert outcome.detail.startswith("1 of 3 batch(es) failed")
    assert out["lib0"].verdict == Verdict.deprecated and out["lib0"].ai_checked
    assert out["lib1"].verdict == Verdict.supported and not out["lib1"].ai_checked
    assert out["lib2"].verdict == Verdict.deprecated and out["lib2"].ai_checked


async def test_an_unexpected_error_in_the_ai_pass_is_contained(monkeypatch, structured, ai_on):
    async def broken(*a, **kw):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(ea, "_ai_pass", broken)
    outcome = ea.AiOutcome()
    out = await ea.enrich(_pkgs(2), today=TODAY, ai_outcome=outcome)

    assert [e.summary for e in out] == ["structured", "structured"]
    assert outcome.status == "failed"
    assert "merge exploded" in outcome.detail


async def test_with_the_toggle_off_no_ai_call_is_made(monkeypatch, structured):
    settings_store.update_runtime_settings({"use_enrichment_agent": False})

    def never(n):
        raise AssertionError("AI must not be called with the toggle off")

    calls = _agent(monkeypatch, never)
    outcome = ea.AiOutcome()
    out = await ea.enrich(_pkgs(2), today=TODAY, ai_outcome=outcome)

    assert calls == [] and len(out) == 2
    assert outcome.status == "off"


async def test_a_scan_whose_ai_fails_is_complete_and_not_degraded(monkeypatch, tmp_path,
                                                                 structured, ai_on):
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(pipeline, "shallow_clone",
                        lambda url, branch, token=None: Clone(path=tmp_path / "repo",
                                                              _tmp=str(tmp_path / "unused")))
    monkeypatch.setattr(Clone, "cleanup", lambda self: None)

    async def fake_extract(clone_path, *, use_agent=False, tag_vars=None):
        return PackageList(packages=_pkgs(2))

    monkeypatch.setattr(pipeline.extraction_agent, "extract", fake_extract)

    def timeout(n):
        raise AgentError("agent timed out after 420.0s")

    _agent(monkeypatch, timeout)
    repo = Repo(id="ai-fails", url="https://example.com/r.git", branches=["main"])
    status = await pipeline.run_scan(repo, "main", use_extraction_agent=False)

    assert status.state.value == "done"
    assert status.degraded is False
    snap = storage.load_index("ai-fails", "main")[0]
    assert snap.degraded is False
    assert snap.ai_status == "failed"
    assert "timed out" in (snap.ai_detail or "")
    # the structured verdicts were kept, so the packages are OK, not UNKNOWN
    assert {f.status.value for f in storage.read_flags("ai-fails", "main", snap.ts)} == {"OK"}
