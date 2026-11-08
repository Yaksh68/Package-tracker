"""An archived GitHub repo only means "deprecated" when the project really
stopped — not when it moved (google-crc32c went into googleapis'
google-cloud-python monorepo and kept releasing on PyPI)."""
import datetime as dt

import httpx
import pytest

from app.agents import enrichment_agent as ea
from app.agents import source_apis as sa
from app.flags import evaluate
from app.models import Ecosystem, Enrichment, FlagStatus, Package, Verdict

REAL_QUERY = sa.query_github_repo  # before the autouse fake replaces it

CRC_DESC = ("This library has moved to "
            "https://github.com/googleapis/google-cloud-python/tree/main/packages/google-crc32c")

REPOS = {
    "googleapis/python-crc32c": {"archived": True, "pushed_at": dt.date(2026, 4, 1),
                                 "moved_to": CRC_DESC.split("moved to ")[1]},
    "googleapis/google-cloud-python": {"archived": False, "pushed_at": dt.date(2026, 10, 12)},
    "brandon-rhodes/backports": {"archived": True, "pushed_at": dt.date(2019, 1, 1)},
    "old/lib": {"archived": True, "pushed_at": dt.date(2024, 1, 1)},
    "old/moved": {"archived": True, "pushed_at": dt.date(2023, 1, 1), "moved_to": "https://github.com/new/home"},
    "new/home": {"archived": True, "pushed_at": dt.date(2024, 1, 1)},
}


@pytest.fixture(autouse=True)
def _fake_github(monkeypatch):
    async def repo(client, slug):
        slug = sa.github_slug(slug) or slug
        r = REPOS.get(slug)
        if r is None:
            return {"found": False}
        return {"found": True, "url": f"https://github.com/{slug}", "slug": slug,
                "default_branch": "main", "moved_to": None, **r}

    async def no_policy(*a, **kw):
        return {"found": False}

    monkeypatch.setattr(sa, "query_github_repo", repo)
    monkeypatch.setattr(sa, "query_github_security_policy", no_policy)


async def _enrich(slug, version, last_release, latest):
    e = Enrichment(name="x", version=version, ecosystem=Ecosystem.pypi, verdict=Verdict.supported,
                   last_release=last_release, latest_version=latest, latest_stable_version=latest)
    await ea._apply_github(None, e, f"https://github.com/{slug}", e.sources)
    return e


def test_moved_to_parses_the_description():
    assert sa.moved_to(CRC_DESC) == CRC_DESC.split("moved to ")[1]
    assert sa.github_slug(sa.moved_to(CRC_DESC)) == "googleapis/google-cloud-python"
    assert sa.moved_to("Moved to https://github.com/a/b.") == "https://github.com/a/b"
    assert sa.moved_to("Deprecated, use requests instead") is None
    assert sa.moved_to(None) is None


async def test_query_github_repo_reports_the_move():
    def handler(request):
        return httpx.Response(200, json={"archived": True, "pushed_at": "2026-04-01T20:26:51Z",
                                         "full_name": "googleapis/python-crc32c",
                                         "html_url": "https://github.com/googleapis/python-crc32c",
                                         "description": CRC_DESC})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await REAL_QUERY(c, "googleapis/python-crc32c")
    assert r["archived"] and r["moved_to"].endswith("/packages/google-crc32c")


async def test_a_rejected_token_is_dropped_for_the_rest_of_the_scan(monkeypatch):
    """A revoked or expired token gets 401 on every call: retry without it,
    say so once, and stop sending it."""
    from app import scan_context, scanlog

    sent = []

    def handler(request):
        auth = request.headers.get("authorization")
        sent.append(auth)
        if auth:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"archived": False, "pushed_at": "2026-04-01T20:26:51Z",
                                         "full_name": "a/b", "html_url": "https://github.com/a/b"})

    ctx = scan_context.ScanContext(workspace_id="local")
    monkeypatch.setattr(ctx, "github_api_token", lambda: ctx._cached("github_api_token", lambda: "gho_revoked"))
    scan_context.bind(ctx)
    log = scanlog.start("r", "main")
    tally = sa.start_error_tally()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            first = await REAL_QUERY(c, "a/b")
            second = await REAL_QUERY(c, "c/d")
    finally:  # don't leak this scan's context into other tests
        scan_context.bind(None)
        scanlog._current.set(None)
        scanlog.clear_repo("r")
        sa._upstream_errors.set(None)
    assert first["found"] and second["found"]
    assert sent == ["Bearer gho_revoked", None, None]  # tried once, then without it
    assert tally == {"api.github.com:auth_rejected": 1}
    warnings = [e.message for e in log.entries if e.level == "warn"]
    assert len(warnings) == 1 and "rejected the token" in warnings[0]


async def test_google_crc32c_moved_into_monorepo_is_not_deprecated():
    e = await _enrich("googleapis/python-crc32c", "1.8.0", dt.date(2026, 9, 24), "1.9.0")
    assert e.archived is False and e.verdict == Verdict.supported
    assert "moved to" in e.sources[0].note
    pkg = Package(name="x", version="1.8.0", ecosystem=Ecosystem.pypi, source_file="requirements.txt")
    assert evaluate(pkg, e, None, today=dt.date(2026, 10, 12)).status == FlagStatus.OK


async def test_released_after_archive_is_not_archived():
    e = await _enrich("old/lib", "2.0", dt.date(2025, 6, 1), "2.1")
    assert e.archived is False and e.verdict == Verdict.supported


async def test_really_abandoned_repo_is_still_deprecated():
    e = await _enrich("brandon-rhodes/backports", "1.0", dt.date(2018, 5, 1), "1.0")
    assert e.archived is True and e.verdict == Verdict.deprecated


async def test_moved_to_another_archived_repo_is_still_deprecated():
    e = await _enrich("old/moved", "1.0", dt.date(2023, 1, 1), "1.0")
    assert e.archived is True and e.verdict == Verdict.deprecated
