"""Every scan attempt leaves one `scan_runs` fact row, failed ones included
(phase 0, task 0.5). This is the data the admin analytics will read."""
import httpx
import pytest

from app import pipeline, storage
from app.agents import enrichment_agent, source_apis
from app.agents.clone import Clone, CloneError
from app.config import get_settings
from app.models import Ecosystem, Package, PackageList, Repo


@pytest.fixture
def fake_clone(monkeypatch, tmp_path):
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(pipeline, "shallow_clone",
                        lambda url, branch, token=None: Clone(path=tmp_path / "repo",
                                                              _tmp=str(tmp_path / "unused")))
    monkeypatch.setattr(Clone, "cleanup", lambda self: None)


def _repo(rid="runs"):
    return Repo(id=rid, url="https://example.com/r.git", branches=["main"])


def _only_run(rid="runs"):
    runs = storage.list_scan_runs(rid)
    assert len(runs) == 1
    return runs[0]


async def test_a_clean_scan_records_its_facts(fake_clone):
    await pipeline.run_scan(_repo(), "main", use_extraction_agent=False, trigger="scheduled")
    run = _only_run()

    assert run.outcome == "done"
    assert run.triggered_by == "scheduled"
    assert run.snapshot_ts == storage.load_index("runs", "main")[0].ts
    assert {"clone", "extract", "inspect", "flag", "snapshot"} <= set(run.stage_seconds)
    assert run.package_count == 0 and run.issue_stages == [] and run.first_error is None
    assert run.finished_at >= run.started_at


async def test_manual_is_the_default_trigger(fake_clone):
    await pipeline.run_scan(_repo(), "main", use_extraction_agent=False)
    assert _only_run().triggered_by == "manual"


async def test_a_failed_clone_is_recorded_as_degraded(monkeypatch):
    def refused(url, branch, token=None):
        raise CloneError("clone https://example.com/r.git@main failed: authentication required")

    monkeypatch.setattr(pipeline, "shallow_clone", refused)
    await pipeline.run_scan(_repo(), "main", use_extraction_agent=False)
    run = _only_run()

    assert run.outcome == "degraded"
    assert run.issue_stages == ["clone"]
    assert "authentication required" in run.first_error
    assert "clone" in run.stage_seconds


async def test_a_crashed_scan_is_recorded_without_a_snapshot(monkeypatch, fake_clone):
    async def boom(*a, **kw):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(pipeline.extraction_agent, "extract", boom)
    await pipeline.run_scan(_repo(), "main", use_extraction_agent=False)
    run = _only_run()

    assert run.outcome == "error"
    assert run.snapshot_ts is None
    assert run.issue_stages == ["scan"]
    assert "extraction exploded" in run.first_error


async def test_package_counts_ai_usage_and_upstream_errors_are_recorded(monkeypatch, fake_clone):
    async def extract(clone_path, *, use_agent=False, tag_vars=None):
        return PackageList(packages=[
            Package(name="a", version="1", ecosystem=Ecosystem.pypi, source_file="requirements.txt"),
            Package(name="b", version="2", ecosystem=Ecosystem.pypi, source_file="requirements.txt"),
            Package(name="c", version="3", ecosystem=Ecosystem.npm, source_file="package.json"),
        ])

    async def enrich(packages, *, ai_outcome=None, **kw):
        source_apis._tally("https://api.github.com/repos/x/y", "rate_limited")
        ai_outcome.status, ai_outcome.detail = "ok", "2 batch(es) researched"
        ai_outcome.batches, ai_outcome.seconds = 2, 3.5
        ai_outcome.tokens = {"input_tokens": 100, "cache_read_input_tokens": 50,
                             "cache_creation_input_tokens": 25, "output_tokens": 40,
                             "web_search_requests": 4, "cost_usd": 0.012}
        return []

    monkeypatch.setattr(pipeline.extraction_agent, "extract", extract)
    monkeypatch.setattr(pipeline.enrichment_agent, "enrich", enrich)
    await pipeline.run_scan(_repo(), "main", use_extraction_agent=False)
    run = _only_run()

    assert run.package_count == 3
    assert run.package_counts == {"pypi": 2, "npm": 1}
    assert run.flag_counts == {"UNKNOWN": 3}
    assert run.ai_status == "ok" and run.ai_batches == 2
    assert run.ai_provider == "claude-cli" and run.ai_model == get_settings().agent_model
    assert (run.ai_tokens_in, run.ai_tokens_out, run.ai_searches) == (175, 40, 4)
    assert run.ai_cost_usd == pytest.approx(0.012)
    assert run.stage_seconds["ai"] == 3.5
    assert run.upstream_errors == {"api.github.com:rate_limited": 1}


async def test_no_ai_usage_is_recorded_when_ai_did_not_run(fake_clone):
    await pipeline.run_scan(_repo(), "main", use_extraction_agent=False)
    run = _only_run()
    assert run.ai_batches == 0 and run.ai_provider is None and run.ai_cost_usd is None


async def test_a_broken_analytics_write_never_breaks_the_scan(monkeypatch, fake_clone):
    def broken(run):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(storage, "write_scan_run", broken)
    status = await pipeline.run_scan(_repo(), "main", use_extraction_agent=False)
    assert status.state.value == "done"


# --------------------------------------------------------------------------- #
# upstream error tally                                                         #
# --------------------------------------------------------------------------- #
async def test_only_real_failures_are_counted():
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.github.com":
            return httpx.Response(403, json={"message": "rate limit"})
        if host == "pypi.org":
            return httpx.Response(503)
        if host == "registry.npmjs.org":
            return httpx.Response(200, content=b"not json")
        if host == "endoflife.date":
            return httpx.Response(404)  # normal: most packages aren't tracked
        raise httpx.ConnectError("no route", request=request)

    tally = source_apis.start_error_tally()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await source_apis._get_json(client, "https://api.github.com/repos/x/y") == {"__status__": 403}
        assert await source_apis._get_json(client, "https://pypi.org/pypi/x/json") == {"__status__": 503}
        assert await source_apis._get_json(client, "https://registry.npmjs.org/x") is None
        assert await source_apis._get_json(client, "https://endoflife.date/api/x.json") == {"__status__": 404}
        assert await source_apis._get_json(client, "https://unreachable.example/x") is None

    assert tally == {
        "api.github.com:rate_limited": 1,
        "pypi.org:server_error": 1,
        "registry.npmjs.org:bad_response": 1,
        "unreachable.example:network": 1,
    }
