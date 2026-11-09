"""Phase 4.3: AI verdict reuse. A finding the AI made for the same
ecosystem:name:version in this workspace's recent scans is applied again
instead of researched again; structured data stays fresh."""
import datetime as dt
import json

from sqlalchemy import update

import app.agents.enrichment_agent as ea
from app import scan_context, settings_store, storage
from app.db import session
from app.db_models import SnapshotRow
from app.models import Confidence, Ecosystem, Enrichment, Repo, SnapshotSummary, Verdict

TODAY = dt.date(2026, 11, 8)
CYBOX_ANSWER = {"name": "cybox", "ecosystem": "pypi", "verdict": "eol", "successor": "stix2",
                "confidence": "high", "summary": "superseded by stix2",
                "sources": [{"url": "https://oasis.example/cti", "note": "OASIS", "origin": "web"}]}


def _ambiguous(name="cybox", version="2.1.0.21"):
    return Enrichment(name=name, version=version, ecosystem=Ecosystem.pypi,
                      verdict=Verdict.unknown, confidence=Confidence.low)


def _earlier_scan(repo_id="earlier", workspace="local", *, days_ago=1, version="2.1.0.21",
                  ts="2026-11-07T10-00-00Z"):
    """A listed snapshot whose cybox finding carries the AI's answer."""
    storage.upsert_repo(Repo(id=repo_id, url=f"https://x/{repo_id}", branches=["main"], workspace_id=workspace))
    found = _ambiguous(version=version)
    ea._merge_ai(found, ea.AiVerdict.model_validate(CYBOX_ANSWER))
    found.ai_result, found.ai_checked = CYBOX_ANSWER, True
    storage.write_enrichment(repo_id, "main", ts, [found])
    storage.upsert_index_entry(repo_id, "main", SnapshotSummary(ts=ts, package_count=1, flag_counts={}))
    with session() as s:
        s.execute(update(SnapshotRow).where(SnapshotRow.repo_id == repo_id).values(
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)))


def _agent(monkeypatch):
    """A fake AI that answers for anything, and records what it was asked."""
    asked: list[str] = []

    class Res:
        is_error, error_message = False, ""

        def __init__(self, names):
            self.text = json.dumps({"verdicts": [{**CYBOX_ANSWER, "name": n, "summary": "researched now"}
                                                 for n in names]})

        def tokens(self):
            return {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    async def fake(prompt, **kw):
        listed = prompt.split("Packages (with signals already gathered):", 1)[1]
        names = [line["name"] for line in json.loads(listed)]
        asked.extend(names)
        return Res(names)

    monkeypatch.setattr(ea, "run_text_agent", fake)
    return asked


async def _research(*pool) -> ea.AiOutcome:
    settings_store.update_runtime_settings({"use_enrichment_agent": True})
    outcome = ea.AiOutcome()
    await ea._ai_pass(list(pool), today=TODAY, outcome=outcome)
    return outcome


async def test_a_recent_finding_is_reused_instead_of_researched(monkeypatch):
    _earlier_scan()
    asked = _agent(monkeypatch)
    cybox, other = _ambiguous(), _ambiguous(name="libfoo")
    outcome = await _research(cybox, other)
    assert asked == ["libfoo"]                       # only the new one went to the AI
    assert cybox.verdict == Verdict.eol and cybox.successor == "stix2"
    assert cybox.ai_checked and cybox.ai_reused_from == "2026-11-07T10-00-00Z"
    assert any(s.url == "https://oasis.example/cti" and s.origin == "web" for s in cybox.sources)
    assert other.ai_result["summary"] == "researched now" and other.ai_reused_from is None
    assert outcome.reused == 1


async def test_everything_reused_means_no_ai_call_at_all(monkeypatch):
    _earlier_scan()
    asked = _agent(monkeypatch)
    outcome = await _research(_ambiguous())
    assert asked == [] and outcome.status == "ok" and "reused 1" in outcome.detail


async def test_a_different_version_or_an_old_finding_is_researched_again(monkeypatch):
    _earlier_scan(days_ago=10)  # outside the default 7 days
    _earlier_scan(repo_id="older-version", version="2.0.0", ts="2026-11-07T11-00-00Z")
    asked = _agent(monkeypatch)
    await _research(_ambiguous())
    assert asked == ["cybox"]


async def test_zero_days_turns_reuse_off(monkeypatch):
    _earlier_scan()
    settings_store.update_runtime_settings({"ai_reuse_days": 0})
    asked = _agent(monkeypatch)
    await _research(_ambiguous())
    assert asked == ["cybox"]


async def test_another_workspaces_findings_are_never_reused(monkeypatch):
    _earlier_scan(repo_id="theirs", workspace="someone-elses")
    scan_context.bind(scan_context.ScanContext(workspace_id="local"))
    try:
        asked = _agent(monkeypatch)
        await _research(_ambiguous())
    finally:
        scan_context.bind(None)
    assert asked == ["cybox"]


def test_the_reuse_window_is_validated():
    import pytest
    for bad in (-1, 91, "7", True):
        with pytest.raises(ValueError):
            settings_store.update_runtime_settings({"ai_reuse_days": bad})
    assert settings_store.update_runtime_settings({"ai_reuse_days": 30})["ai_reuse_days"] == 30
