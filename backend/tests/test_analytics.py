"""Phase 6: the admin analytics. Who can see them, and each panel's numbers
against seeded data with known counts. The panels are called directly (no
HTTP) where only the numbers matter, so test requests don't add activity."""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app import accounts, analytics, crypto, storage
from app.config import get_settings
from app.db import session
from app.db_models import SystemSampleRow, UsageEventRow, UserActivityDayRow, UserRow
from app.main import app
from app.models import (Ecosystem, Enrichment, Flag, FlagStatus, Repo, ScanRunRecord, SnapshotSummary,
                        Verdict)

CSRF = {"X-Requested-With": "packages-tracker"}
TODAY = datetime.now(timezone.utc).date()


def _at(days_ago: int, hour: int = 12) -> datetime:
    d = TODAY - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


def _week(days: int = 7) -> analytics.Range:
    """The last `days` days, today included."""
    return analytics.date_range(TODAY - timedelta(days=days - 1), TODAY)


_ids = iter(range(7000, 1_000_000))


def _person(login: str, *, joined: int = 0, admin: bool = False):
    """A GitHub user who signed up `joined` days ago."""
    user, _ = accounts.sign_in_github_user({"id": next(_ids), "login": login}, f"gho_{login}", admin=admin)
    with session() as s:
        s.execute(update(UserRow).where(UserRow.id == user.id).values(created_at=_at(joined, 9)))
    return user


def _personal(user) -> str:
    return next(w.id for w in accounts.user_workspaces(user.id) if w.personal)


def _active(user, *days_ago: int) -> None:
    with session() as s:
        for d in days_ago:
            s.add(UserActivityDayRow(user_id=user.id, day=TODAY - timedelta(days=d)))


def _event(event: str, user=None, *, days_ago: int = 0, workspace: str | None = None, **meta) -> None:
    with session() as s:
        s.add(UsageEventRow(at=_at(days_ago), event=event, user_id=user.id if user else None,
                            workspace_id=workspace, meta=meta))


def _run(days_ago: int = 0, *, outcome: str = "done", repo: str = "r1", branch: str = "main",
         hour: int = 12, queued_secs: int | None = None, **fields) -> None:
    started = _at(days_ago, hour)
    storage.write_scan_run(ScanRunRecord(
        repo_id=repo, branch=branch, started_at=started, finished_at=started + timedelta(seconds=60),
        queued_at=started - timedelta(seconds=queued_secs) if queued_secs is not None else None,
        outcome=outcome, **fields))


def _tiles(r: analytics.Range) -> dict:
    return {t["key"]: t for t in analytics.overview(r)["tiles"]}


# --------------------------------------------------------------------------- #
# access                                                                        #
# --------------------------------------------------------------------------- #
@pytest.fixture
def public(monkeypatch):
    for k, v in {"AUTH_MODE": "github", "PLATFORM_ADMINS": "root"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    crypto.reset()


def _client(user) -> TestClient:
    c = TestClient(app, headers=CSRF)
    c.cookies.set("pt_session", accounts.create_session(user.id))
    return c


def test_only_the_platform_admin_sees_the_analytics(public):
    assert _client(_person("nobody")).get("/api/admin/analytics/overview").status_code == 404
    root = _client(_person("root", admin=True))
    for name in analytics.PANELS:
        res = root.get(f"/api/admin/analytics/{name}")
        assert res.status_code == 200, (name, res.text)
        assert res.json()["range"]["days"] == analytics.DEFAULT_DAYS
    assert root.get("/api/admin/analytics/nothing-here").status_code == 404
    backwards = {"start": TODAY.isoformat(), "end": (TODAY - timedelta(days=3)).isoformat()}
    assert root.get("/api/admin/analytics/overview", params=backwards).status_code == 422
    too_long = {"start": (TODAY - timedelta(days=400)).isoformat(), "end": TODAY.isoformat()}
    assert root.get("/api/admin/analytics/overview", params=too_long).status_code == 422


def test_the_range_and_the_period_before_it():
    r = analytics.date_range(date(2026, 10, 1), date(2026, 10, 10))
    assert r.days == 10 and r.previous == analytics.Range(date(2026, 9, 21), date(2026, 9, 30))
    assert analytics.date_range(end=TODAY + timedelta(days=5)).end == TODAY  # never in the future
    assert analytics.date_range().days == analytics.DEFAULT_DAYS


# --------------------------------------------------------------------------- #
# the panels                                                                    #
# --------------------------------------------------------------------------- #
def test_the_headline_numbers_and_their_change():
    alice = _person("alice", joined=2)    # this week
    bob = _person("bob", joined=10)       # the week before
    carol = _person("carol", joined=30)   # before both
    _active(alice, 2, 1, 0)
    _active(bob, 10, 3)
    _active(carol, 20, 8)
    _run(0, ai_cost_usd=0.5, ai_batches=1)
    _run(1, outcome="error")
    _run(5, ai_cost_usd=0.25, ai_batches=1)
    _run(9)
    _event("repo_added", alice, days_ago=1)
    _event("repo_added", bob, days_ago=9)

    t = _tiles(_week())
    assert (t["users"]["value"], t["users"]["previous"]) == (3, 2)
    assert (t["signups"]["value"], t["signups"]["previous"]) == (1, 1)
    assert (t["active_users"]["value"], t["active_users"]["previous"]) == (2, 2)
    assert (t["scans"]["value"], t["scans"]["previous"]) == (3, 1)
    assert (t["failed_scans"]["value"], t["failed_scans"]["previous"]) == (1, 0)
    assert t["failed_scans"]["better"] == "lower"
    assert (t["repos_added"]["value"], t["repos_added"]["previous"]) == (1, 1)
    assert (t["ai_cost_usd"]["value"], t["ai_cost_usd"]["previous"]) == (0.75, 0)
    assert len(t["scans"]["trend"]) == analytics.TREND_DAYS
    assert sum(p["value"] for p in t["scans"]["trend"]) == 4
    assert t["users"]["trend"][-1] == {"day": TODAY.isoformat(), "value": 3}


def test_growth_day_by_day():
    alice = _person("alice", joined=2)
    bob = _person("bob", joined=10)
    _active(alice, 2, 1)
    _active(bob, 10, 3)
    _event("workspace_created", alice, days_ago=1)
    _event("repo_added", alice)
    _event("repo_added", bob)

    days = {row["day"]: row for row in analytics.growth(_week())["days"]}
    assert len(days) == 7
    first, two_ago, yesterday, today = (days[(TODAY - timedelta(days=n)).isoformat()] for n in (6, 2, 1, 0))
    assert (first["users"], first["signups"], first["active_7d"]) == (1, 0, 1)  # bob, active 10 days ago
    assert (two_ago["users"], two_ago["signups"], two_ago["active"]) == (2, 1, 1)
    assert yesterday["workspaces_created"] == 1
    assert (today["repos_added"], today["active"], today["active_7d"]) == (2, 0, 2)


def test_activation_counts_what_each_sign_up_did():
    dan = _person("dan", joined=3)
    erin = _person("erin", joined=1)
    frank = _person("frank", joined=20)   # signed up before the range
    _event("repo_added", dan)
    _event("ai_toggle", dan, on=True)
    _event("ai_toggle", erin, on=False)  # turning it off isn't turning it on
    _event("invite_sent", erin, kind="link")
    _event("repo_added", frank)
    _active(dan, 3, 2)
    _active(erin, 1)
    _run(1, workspace_id=_personal(dan))
    _run(0, workspace_id=_personal(erin), outcome="error")

    out = analytics.activation(_week())
    steps = {s["key"]: (s["users"], s["pct"]) for s in out["steps"]}
    assert out["cohort"] == 2
    assert steps == {"signed_up": (2, 100.0), "added_repo": (1, 50.0), "got_results": (1, 50.0),
                     "came_back": (1, 50.0), "turned_on_ai": (1, 50.0), "invited": (1, 50.0)}


def test_retention_by_weekly_cohort():
    this_monday = TODAY - timedelta(days=TODAY.weekday())
    week = this_monday - timedelta(weeks=2)
    gina = _person("gina", joined=(TODAY - week).days)          # the cohort's Monday
    hugo = _person("hugo", joined=(TODAY - week).days - 1)      # its Tuesday
    _active(gina, (TODAY - week).days, (TODAY - this_monday).days)
    _active(hugo, (TODAY - week).days - 1)

    cohorts = {c["week"]: c for c in analytics.retention(_week())["cohorts"]}
    assert len(cohorts) == analytics.RETENTION_WEEKS
    assert cohorts[week.isoformat()] == {"week": week.isoformat(), "users": 2, "weeks": [100.0, 0.0, 50.0]}
    assert cohorts[this_monday.isoformat()]["weeks"] == [None]  # nobody signed up this week


def test_scans_results_stages_and_failures():
    _run(0, triggered_by="manual", queued_secs=30, stage_seconds={"clone": 2, "extract": 4, "flag": 1},
         upstream_errors={"api.github.com:rate_limited": 2})
    _run(0, outcome="degraded", hour=13, triggered_by="scheduled", stage_seconds={"clone": 4, "extract": 6},
         issue_stages=["enrich"], upstream_errors={"api.github.com:rate_limited": 1, "pypi.org:timeout": 1})
    _run(1, outcome="error", triggered_by="scheduled", queued_secs=90, stage_seconds={"clone": 10},
         issue_stages=["clone"], first_error="could not clone")
    _run(8)  # before the range

    out = analytics.scans(_week())
    assert out["totals"] == {"runs": 3, "done": 1, "degraded": 1, "error": 1, "success_pct": 66.7}
    days = {d["day"]: d for d in out["days"]}
    assert days[TODAY.isoformat()] == {"day": TODAY.isoformat(), "done": 1, "degraded": 1, "error": 0}
    assert days[(TODAY - timedelta(days=1)).isoformat()]["error"] == 1
    assert out["triggers"] == [{"trigger": "scheduled", "runs": 2}, {"trigger": "manual", "runs": 1}]
    assert out["stages"] == [
        {"stage": "clone", "runs": 3, "median": 4.0, "p90": 10.0},
        {"stage": "extract", "runs": 2, "median": 5.0, "p90": 6.0},
        {"stage": "flag", "runs": 1, "median": 1.0, "p90": 1.0},
    ]
    assert out["queue_wait"] == {"median": 60.0, "p90": 90.0}
    assert out["duration"] == {"median": 60.0, "p90": 60.0}
    assert out["failures"] == [{"stage": "clone", "error": 1, "degraded": 0},
                               {"stage": "enrich", "error": 0, "degraded": 1}]
    assert out["upstream_errors"] == [{"host": "api.github.com", "kind": "rate_limited", "count": 3},
                                      {"host": "pypi.org", "kind": "timeout", "count": 1}]


def _snapshot(repo_id: str, ts: str, findings: list[tuple[Ecosystem, str, FlagStatus]]) -> None:
    """A listed snapshot of repo_id@main holding these packages and flags."""
    items = [Enrichment(name=name, version="1.0", ecosystem=eco, verdict=Verdict.eol)
             for eco, name, _ in findings]
    storage.write_enrichment(repo_id, "main", ts, items)
    storage.write_flags(repo_id, "main", ts, [Flag(key=e.key, status=status)
                                              for e, (_, _, status) in zip(items, findings)])
    storage.upsert_index_entry(repo_id, "main", SnapshotSummary(ts=ts, package_count=len(items), flag_counts={}))


def test_what_people_scan_and_the_common_eol_threshold():
    eol = FlagStatus.ALREADY_EOL
    for i in range(6):
        ws = _personal(_person(f"user{i}"))
        repo = f"repo{i}"
        storage.upsert_repo(Repo(id=repo, url=f"https://github.com/o/{repo}", branches=["main"], workspace_id=ws))
        if i < 5:  # Django is past its end of life in 5 workspaces; left-pad in 4
            found = [(Ecosystem.pypi, "Django", eol)] + ([(Ecosystem.npm, "left-pad", eol)] if i < 4 else [])
            _snapshot(repo, "2026-11-01T10-00-00Z", found)
        else:  # the 6th fixed it: only its older scan had it
            _snapshot(repo, "2026-10-01T10-00-00Z", [(Ecosystem.pypi, "Django", eol)])
            _snapshot(repo, "2026-11-01T10-00-00Z", [(Ecosystem.pypi, "Django", FlagStatus.OK)])

    _run(2, repo="a", package_counts={"pypi": 10}, flag_counts={"OK": 10})
    _run(1, repo="a", package_counts={"pypi": 12, "npm": 3}, flag_counts={"ALREADY_EOL": 2, "OK": 13})
    _run(0, repo="b", outcome="error", package_counts={"npm": 99})  # failed: ignored
    _run(1, repo="b", package_counts={"npm": 5}, flag_counts={"DEPRECATED": 1, "OK": 4})

    out = analytics.content(_week())
    assert out["branches"] == 2
    assert out["ecosystems"] == [{"ecosystem": "pypi", "packages": 12, "repos": 1},
                                 {"ecosystem": "npm", "packages": 8, "repos": 2}]
    assert out["flags"] == [{"status": "ALREADY_EOL", "packages": 2}, {"status": "DEPRECATED", "packages": 1},
                            {"status": "OK", "packages": 17}]
    assert out["common_eol"] == [{"ecosystem": "pypi", "name": "Django", "workspaces": 5, "repos": 5}]
    assert out["common_min_workspaces"] == 5


def test_ai_use_spend_errors_and_results():
    ai = {"ai_batches": 1}
    _run(0, ai_status="ok", ai_provider="anthropic", ai_model="claude-x", ai_batches=2, ai_tokens_in=1000,
         ai_tokens_out=200, ai_searches=3, ai_cost_usd=0.02)
    _run(0, ai_status="partial", ai_provider="openai-compatible:groq", ai_model="llama", ai_batches=3,
         ai_failed_batches=1, ai_tokens_in=500, ai_tokens_out=100, ai_searches=1, ai_cost_usd=None,
         ai_detail="1 of 3 batch(es) failed: timed out")
    _run(1, ai_status="quota", ai_provider="anthropic", ai_model="claude-x", ai_tokens_in=100,
         ai_cost_usd=0.001, ai_detail="1 of 1 batch(es) failed: rate limit or quota reached", **ai)
    _run(1, ai_status="failed", ai_provider="anthropic", ai_cost_usd=0.0, ai_batches=2,
         ai_detail="2 of 2 batch(es) failed: authentication failed — check the API key or the Claude CLI login")
    _run(2, ai_status="off")
    _run(2, ai_status="no_key")
    _run(20, ai_status="ok", ai_provider="anthropic", ai_cost_usd=9.0, **ai)  # before the range

    answer = {"name": "x", "ecosystem": "pypi", "verdict": "eol"}
    found = Enrichment(name="a", ecosystem=Ecosystem.pypi, ai_result=answer, ai_checked=True)
    unsure = Enrichment(name="b", ecosystem=Ecosystem.pypi, ai_result={**answer, "verdict": "unknown"},
                        ai_checked=True)
    reused = Enrichment(name="c", ecosystem=Ecosystem.pypi, ai_result=answer, ai_checked=True,
                        ai_reused_from="2026-11-01T10-00-00Z")
    untouched = Enrichment(name="d", ecosystem=Ecosystem.pypi)
    storage.write_enrichment("r1", "main", "2026-11-09T10-00-00Z", [found, unsure, reused, untouched])

    out = analytics.ai(_week())
    assert [s["status"] for s in out["statuses"]] == ["ok", "partial", "quota", "failed", "no_key", "off"]
    assert {e["kind"]: e["runs"] for e in out["errors"]} == {"timeout": 1, "quota": 1, "auth": 1}
    assert out["totals"] == {"runs": 4, "tokens_in": 1600, "tokens_out": 300, "searches": 4,
                             "cost_usd": 0.021, "scans": 6, "unpriced_runs": 1}
    anthropic, groq = out["providers"]
    assert (anthropic["provider"], anthropic["runs"], anthropic["cost_usd"]) == ("anthropic", 3, 0.021)
    assert anthropic["models"] == ["claude-x"] and anthropic["unpriced_runs"] == 0
    assert (groq["provider"], groq["runs"], groq["unpriced_runs"]) == ("openai-compatible:groq", 1, 1)
    today = next(d for d in out["days"] if d["day"] == TODAY.isoformat())
    assert (today["runs"], today["tokens_in"], today["searches"], today["cost_usd"]) == (2, 1500, 4, 0.02)
    assert {r["key"]: r["packages"] for r in out["results"]} == {"answered": 1, "unknown": 1, "reused": 1}


def test_the_workspaces_table():
    owner = _person("owner")
    helper = _person("helper")
    ws = accounts.create_workspace("Team", owner.id).id
    accounts.add_member(ws, helper.id, "editor")
    for repo in ("one", "two"):
        storage.upsert_repo(Repo(id=repo, url=f"https://github.com/o/{repo}", branches=["main"], workspace_id=ws))
    _active(owner, 1)
    _run(0, workspace_id=ws, ai_cost_usd=0.5, ai_batches=1)
    _run(1, workspace_id=ws, outcome="error")
    _run(20, workspace_id=ws)
    accounts.set_workspace_settings(ws, {"scans_paused_at": _at(0).isoformat()})

    rows = analytics.workspaces(_week())["workspaces"]
    top = rows[0]
    assert top["id"] == ws and top["personal"] is False
    assert {k: top[k] for k in ("members", "active_members", "repos", "scans", "failed_scans", "ai_cost_usd",
                                "paused")} == {"members": 2, "active_members": 1, "repos": 2, "scans": 2,
                                               "failed_scans": 1, "ai_cost_usd": 0.5, "paused": True}
    assert top["last_scan_at"].startswith(TODAY.isoformat())
    assert rows[1]["id"] == _personal(helper) and rows[1]["scans"] == 0


def test_system_health_is_sampled_and_old_samples_go():
    with session() as s:
        s.add(SystemSampleRow(at=datetime.now(timezone.utc) - timedelta(days=analytics.SAMPLE_KEEP_DAYS + 5),
                              queue_waiting=0, scans_running=0))
    analytics.record_sample()
    with session() as s:
        assert s.scalar(select(func.count()).select_from(SystemSampleRow)) == 1  # the old one went

    out = analytics.system(_week(1))
    now = out["now"]
    assert now["disk_total_bytes"] > 0 and now["db_bytes"] > 0
    assert (now["queue_waiting"], now["scans_running"]) == (0, 0)
    assert out["per"] == "hour" and len(out["samples"]) == 1
    assert analytics.system(_week(30))["per"] == "day"


def test_panels_are_cached_for_a_while(monkeypatch):
    r = _week()
    _person("first", joined=1)
    first = analytics.panel("overview", r)
    _person("second", joined=1)
    assert analytics.panel("overview", r) is first  # within CACHE_SECONDS: the same answer
    monkeypatch.setattr(analytics, "CACHE_SECONDS", 0)
    again = {t["key"]: t["value"] for t in analytics.panel("overview", r)["tiles"]}
    assert again["users"] == 2
    assert analytics.panel("system", r) is not analytics.panel("system", r)  # always measured fresh
