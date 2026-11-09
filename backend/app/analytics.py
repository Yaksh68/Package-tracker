"""Admin analytics (phase 6): how the whole service is doing, for the
platform admin. The API (api/analytics.py) sits under /api/admin, so everyone
else gets a 404.

Privacy: totals and metadata only. Nothing here returns a workspace's scan
results, its package lists or its keys. The one list of package names (common
end-of-life packages) only names a package found in at least
COMMON_MIN_WORKSPACES workspaces, so it can't point at one workspace. A
workspace's details still come from /api/admin/workspaces/{id}, which is
recorded in that workspace's audit log.

The numbers come from the fact tables (scan_runs, user_activity_days,
usage_events, system_samples) and the main tables. Each panel selects only
the columns it needs and adds them up in Python: there are few users (the
sign-up cap), and it reads the same on SQLite and PostgreSQL. Results are
cached for CACHE_SECONDS per panel and date range. Days are UTC days.
"""
from __future__ import annotations

import bisect
import logging
import math
import os
import shutil
import statistics
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, delete, func, select, text
from sqlalchemy.orm import Session

from . import scan_queue
from .agents.enrichment_agent import _KIND_TEXT as AI_ERROR_TEXT  # the wording ai_detail uses
from .config import get_settings
from .db import is_postgres, session
from .db_models import (EnrichmentRow, FlagRow, MembershipRow, RepoRow, ScanRunRow, SnapshotRow,
                        SystemSampleRow, UsageEventRow, UserActivityDayRow, UserRow, WorkspaceRow,
                        WorkspaceSettingRow)

log = logging.getLogger("packages-tracker.analytics")

MAX_DAYS = 366             # the longest date range a panel takes
DEFAULT_DAYS = 30
TREND_DAYS = 30            # the headline tiles' mini trend line
RETENTION_WEEKS = 8
COMMON_MIN_WORKSPACES = 5  # privacy: a package is named only when this many workspaces have it
COMMON_LIMIT = 20
SAMPLE_KEEP_DAYS = 400
CACHE_SECONDS = 300


# --------------------------------------------------------------------------- #
# Dates                                                                        #
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything stored is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day(dt: datetime) -> date:
    return _utc(dt).date()


def _iso(dt: datetime | None) -> str | None:
    return _utc(dt).isoformat() if dt else None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass(frozen=True)
class Range:
    """A span of whole UTC days, both ends included."""
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def previous(self) -> Range:
        """The same number of days just before it, for "change since"."""
        end = self.start - timedelta(days=1)
        return Range(end - timedelta(days=self.days - 1), end)

    @property
    def lo(self) -> datetime:
        return _midnight(self.start)

    @property
    def hi(self) -> datetime:
        """Exclusive: the midnight after the last day."""
        return _midnight(self.end + timedelta(days=1))

    def each_day(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]

    def __contains__(self, d: date) -> bool:
        return self.start <= d <= self.end

    def as_dict(self) -> dict:
        p = self.previous
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days,
                "previous": {"start": p.start.isoformat(), "end": p.end.isoformat()}}


def date_range(start: date | None = None, end: date | None = None) -> Range:
    """The days a panel covers: the last DEFAULT_DAYS unless given. The end
    can't be in the future. ValueError for a backwards or too long range."""
    today = _now().date()
    end = min(end or today, today)
    start = start or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise ValueError("the start date must be on or before the end date")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"a date range can cover at most {MAX_DAYS} days")
    return Range(start, end)


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #
def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _p90(values: list[float]) -> float | None:
    """Nearest-rank 90th percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)], 1)


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _daily(days: Iterable[date], over: list[date]) -> list[dict]:
    counts = Counter(days)
    return [{"day": d.isoformat(), "value": counts.get(d, 0)} for d in over]


def _users(s: Session) -> list[tuple[str, datetime]]:
    """Real accounts (GitHub sign-ins) and when they signed up. The built-in
    local user isn't a person."""
    rows = s.execute(select(UserRow.id, UserRow.created_at).where(UserRow.github_id.is_not(None)))
    return [(uid, _utc(created)) for uid, created in rows]


def _active_days(s: Session, start: date, end: date, users: set[str]) -> dict[date, set[str]]:
    """Which of these users used the app on each day from start to end."""
    out: dict[date, set[str]] = defaultdict(set)
    rows = s.execute(select(UserActivityDayRow.user_id, UserActivityDayRow.day)
                     .where(UserActivityDayRow.day >= start, UserActivityDayRow.day <= end))
    for uid, day in rows:
        if uid in users:
            out[day].add(uid)
    return out


def _runs(s: Session, start: date, end: date, *columns) -> list:
    """These columns of the scan_runs that started from start to end."""
    return s.execute(select(*columns).where(
        ScanRunRow.started_at >= _midnight(start),
        ScanRunRow.started_at < _midnight(end + timedelta(days=1)))).all()


def _events(s: Session, events: Iterable[str], start: date, end: date) -> list:
    return s.execute(select(UsageEventRow.event, UsageEventRow.at, UsageEventRow.user_id)
                     .where(UsageEventRow.event.in_(tuple(events)),
                            UsageEventRow.at >= _midnight(start),
                            UsageEventRow.at < _midnight(end + timedelta(days=1)))).all()


# --------------------------------------------------------------------------- #
# Panels                                                                       #
# --------------------------------------------------------------------------- #
def _tile(key: str, label: str, value, previous, trend: list[dict], **extra) -> dict:
    return {"key": key, "label": label, "value": value, "previous": previous, "trend": trend, **extra}


def overview(r: Range) -> dict:
    """The headline numbers: each for the range and the period before it, with
    a daily trend over the last TREND_DAYS days of the range."""
    p = r.previous
    trend = Range(r.end - timedelta(days=TREND_DAYS - 1), r.end)
    lo = min(p.start, trend.start)
    with session() as s:
        users = _users(s)
        active = _active_days(s, lo, r.end, {uid for uid, _ in users})
        runs = _runs(s, lo, r.end, ScanRunRow.started_at, ScanRunRow.outcome, ScanRunRow.ai_cost_usd)
        added = [_day(at) for _, at, _ in _events(s, ("repo_added",), lo, r.end)]
        trials = [_day(at) for _, at, _ in _events(s, ("trial_scan",), lo, r.end)]
    joined = sorted(created.date() for _, created in users)
    scanned = [(_day(at), outcome, cost or 0.0) for at, outcome, cost in runs]
    scan_days = [d for d, _, _ in scanned]
    failed = [d for d, outcome, _ in scanned if outcome == "error"]
    spend_by_day: dict[date, float] = defaultdict(float)
    for d, _, cost in scanned:
        spend_by_day[d] += cost
    days = trend.each_day()

    def users_by(d: date) -> int:
        return bisect.bisect_right(joined, d)

    def within(rng: Range, ds: Iterable[date]) -> int:
        return sum(1 for d in ds if d in rng)

    def actives(rng: Range) -> int:
        return len(set().union(*(active.get(d, set()) for d in rng.each_day())))

    def spend(rng: Range) -> float:
        return round(sum(v for d, v in spend_by_day.items() if d in rng), 4)

    return {"tiles": [
        _tile("users", "Users", users_by(r.end), users_by(p.end),
              [{"day": d.isoformat(), "value": users_by(d)} for d in days]),
        _tile("signups", "Sign-ups", within(r, joined), within(p, joined), _daily(joined, days)),
        _tile("active_users", "Active users", actives(r), actives(p),
              [{"day": d.isoformat(), "value": len(active.get(d, ()))} for d in days]),
        _tile("scans", "Scans", within(r, scan_days), within(p, scan_days), _daily(scan_days, days)),
        _tile("failed_scans", "Failed scans", within(r, failed), within(p, failed), _daily(failed, days),
              better="lower"),
        _tile("repos_added", "Repositories added", within(r, added), within(p, added), _daily(added, days)),
        _tile("trial_scans", "Free scans (no account)", within(r, trials), within(p, trials),
              _daily(trials, days)),
        _tile("ai_cost_usd", "AI spend (estimated)", spend(r), spend(p),
              [{"day": d.isoformat(), "value": round(spend_by_day.get(d, 0.0), 4)} for d in days], unit="usd"),
    ]}


def growth(r: Range) -> dict:
    """Day by day: users so far, sign-ups, active users (that day and over
    the 7 days to it), and team workspaces and repositories added."""
    lo = r.start - timedelta(days=6)  # the first day's 7-day count looks back a week
    with session() as s:
        users = _users(s)
        active = _active_days(s, lo, r.end, {uid for uid, _ in users})
        events = _events(s, ("workspace_created", "repo_added"), r.start, r.end)
    joined = sorted(created.date() for _, created in users)
    new = Counter(joined)
    made = Counter((event, _day(at)) for event, at, _ in events)
    rows = []
    for d in r.each_day():
        week = set().union(*(active.get(d - timedelta(days=i), set()) for i in range(7)))
        rows.append({
            "day": d.isoformat(),
            "users": bisect.bisect_right(joined, d),
            "signups": new.get(d, 0),
            "active": len(active.get(d, ())),
            "active_7d": len(week),
            "workspaces_created": made.get(("workspace_created", d), 0),
            "repos_added": made.get(("repo_added", d), 0),
        })
    return {"days": rows}


ACTIVATION_STEPS = (
    ("signed_up", "Signed up"),
    ("added_repo", "Added a repository"),
    ("got_results", "Got scan results"),
    ("came_back", "Came back on another day"),
    ("turned_on_ai", "Turned on AI research"),
    ("invited", "Invited someone"),
)


def activation(r: Range) -> dict:
    """What the people who signed up in the range went on to do, at any time
    since. Each step is counted on its own, not as a strict funnel."""
    with session() as s:
        cohort = s.execute(select(UserRow.id, UserRow.created_at).where(
            UserRow.github_id.is_not(None), UserRow.created_at >= r.lo, UserRow.created_at < r.hi)).all()
        ids = [uid for uid, _ in cohort]
        done: dict[str, set[str]] = {key: set() for key, _ in ACTIVATION_STEPS}
        done["signed_up"] = set(ids)
        if ids:
            for uid, event, meta in s.execute(
                    select(UsageEventRow.user_id, UsageEventRow.event, UsageEventRow.meta)
                    .where(UsageEventRow.user_id.in_(ids),
                           UsageEventRow.event.in_(("repo_added", "ai_toggle", "invite_sent")))):
                if event == "repo_added":
                    done["added_repo"].add(uid)
                elif event == "invite_sent":
                    done["invited"].add(uid)
                elif (meta or {}).get("on"):
                    done["turned_on_ai"].add(uid)
            memberships = s.execute(select(MembershipRow.user_id, MembershipRow.workspace_id)
                                    .where(MembershipRow.user_id.in_(ids))).all()
            with_results = set(s.scalars(select(ScanRunRow.workspace_id).distinct().where(
                ScanRunRow.outcome != "error",
                ScanRunRow.workspace_id.in_({ws for _, ws in memberships}))))
            done["got_results"] = {uid for uid, ws in memberships if ws in with_results}
            joined = {uid: _day(created) for uid, created in cohort}
            for uid, day in s.execute(select(UserActivityDayRow.user_id, UserActivityDayRow.day)
                                      .where(UserActivityDayRow.user_id.in_(ids))):
                if day > joined[uid]:
                    done["came_back"].add(uid)
    total = len(ids)
    return {"cohort": total, "steps": [
        {"key": key, "label": label, "users": len(done[key]), "pct": _pct(len(done[key]), total)}
        for key, label in ACTIVATION_STEPS]}


def retention(r: Range) -> dict:
    """Weekly cohorts (weeks start on Monday): of the people who signed up in
    a week, the % who used the app in that week and each one after it. The
    last RETENTION_WEEKS cohorts up to the range's end."""
    last = _monday(r.end)
    first = last - timedelta(weeks=RETENTION_WEEKS - 1)
    with session() as s:
        users = [(uid, created.date()) for uid, created in _users(s) if first <= created.date() <= r.end]
        active = _active_days(s, first, r.end, {uid for uid, _ in users})
    weeks_of: dict[str, set[date]] = defaultdict(set)
    for day, who in active.items():
        for uid in who:
            weeks_of[uid].add(_monday(day))
    rows = []
    for i in range(RETENTION_WEEKS):
        week = first + timedelta(weeks=i)
        members = [uid for uid, joined in users if _monday(joined) == week]
        cells = []
        for k in range(RETENTION_WEEKS - i):
            w = week + timedelta(weeks=k)
            if w > r.end:
                break
            cells.append(_pct(sum(1 for uid in members if w in weeks_of[uid]), len(members)))
        rows.append({"week": week.isoformat(), "users": len(members), "weeks": cells})
    return {"cohorts": rows}


STAGES = ("clone", "extract", "inspect", "enrich", "ai", "flag", "snapshot")  # pipeline order
OUTCOMES = ("done", "degraded", "error")


def scans(r: Range) -> dict:
    """Scan results per day, what started them, time per stage and in the
    queue, where failures happened, and which outside services failed."""
    with session() as s:
        rows = _runs(s, r.start, r.end, ScanRunRow.started_at, ScanRunRow.finished_at, ScanRunRow.queued_at,
                     ScanRunRow.outcome, ScanRunRow.triggered_by, ScanRunRow.stage_seconds,
                     ScanRunRow.issue_stages, ScanRunRow.upstream_errors)
    per_day: dict[date, Counter] = defaultdict(Counter)
    triggers: Counter = Counter()
    stage_times: dict[str, list[float]] = defaultdict(list)
    durations: list[float] = []
    waits: list[float] = []
    failures: dict[str, Counter] = defaultdict(Counter)
    upstream: Counter = Counter()
    for started, finished, queued, outcome, trigger, stages, issues, errors in rows:
        started, finished, queued = _utc(started), _utc(finished), _utc(queued)
        per_day[started.date()][outcome] += 1
        triggers[trigger] += 1
        for stage, secs in (stages or {}).items():
            if isinstance(secs, (int, float)):
                stage_times[stage].append(float(secs))
        if finished:
            durations.append((finished - started).total_seconds())
        if queued:
            waits.append(max(0.0, (started - queued).total_seconds()))
        if outcome != "done":
            failures[(issues or ["unknown"])[0]][outcome] += 1
        for source, n in (errors or {}).items():
            upstream[source] += int(n or 0)
    totals: Counter = Counter()
    for c in per_day.values():
        totals.update(c)
    order = list(STAGES) + sorted(set(stage_times) - set(STAGES))
    return {
        "totals": {"runs": len(rows), **{o: totals.get(o, 0) for o in OUTCOMES},
                   "success_pct": _pct(totals.get("done", 0) + totals.get("degraded", 0), len(rows))},
        "days": [{"day": d.isoformat(), **{o: per_day[d].get(o, 0) for o in OUTCOMES}} for d in r.each_day()],
        "triggers": [{"trigger": t, "runs": n} for t, n in triggers.most_common()],
        "stages": [{"stage": st, "runs": len(stage_times[st]), "median": _median(stage_times[st]),
                    "p90": _p90(stage_times[st])} for st in order if stage_times.get(st)],
        "duration": {"median": _median(durations), "p90": _p90(durations)},
        "queue_wait": {"median": _median(waits), "p90": _p90(waits)},
        "failures": sorted(({"stage": st, "error": c.get("error", 0), "degraded": c.get("degraded", 0)}
                            for st, c in failures.items()),
                           key=lambda f: (-(f["error"] + f["degraded"]), f["stage"])),
        "upstream_errors": [{"host": src.rpartition(":")[0] or src, "kind": src.rpartition(":")[2] if ":" in src else "",
                             "count": n} for src, n in upstream.most_common(15)],
    }


# Worst first. VULNERABLE is retired, but old rows may still carry it.
FLAG_ORDER = ("ALREADY_EOL", "VULNERABLE", "EOS_PASSED", "EOL_BEFORE_RELEASE", "EOL_SOON_AFTER_RELEASE",
              "DEPRECATED", "ARCHIVED", "STALE", "UNKNOWN", "OK", "IGNORED")
EOL_FLAGS = ("ALREADY_EOL", "EOS_PASSED")


def content(r: Range) -> dict:
    """What people scan: the latest scan in the range of each branch, added
    up by ecosystem and by flag; and the end-of-life packages that many
    workspaces share."""
    with session() as s:
        rows = _runs(s, r.start, r.end, ScanRunRow.repo_id, ScanRunRow.branch, ScanRunRow.started_at,
                     ScanRunRow.outcome, ScanRunRow.package_counts, ScanRunRow.flag_counts)
        common = _common_eol(s)
    latest: dict[tuple[str, str], tuple] = {}
    for repo, branch, started, outcome, packages, flags in rows:
        if outcome == "error":
            continue
        started = _utc(started)
        if (repo, branch) not in latest or started > latest[(repo, branch)][0]:
            latest[(repo, branch)] = (started, repo, packages or {}, flags or {})
    packages: Counter = Counter()
    repos: dict[str, set[str]] = defaultdict(set)
    flags: Counter = Counter()
    for _, repo, by_eco, by_flag in latest.values():
        for eco, n in by_eco.items():
            packages[eco] += int(n or 0)
            repos[eco].add(repo)
        for status, n in by_flag.items():
            flags[status] += int(n or 0)
    order = [f for f in FLAG_ORDER if flags.get(f)] + sorted(f for f in flags if f not in FLAG_ORDER and flags[f])
    return {
        "branches": len(latest),
        "ecosystems": [{"ecosystem": eco, "packages": n, "repos": len(repos[eco])}
                       for eco, n in packages.most_common()],
        "flags": [{"status": f, "packages": flags[f]} for f in order],
        "common_eol": common,
        "common_min_workspaces": COMMON_MIN_WORKSPACES,
    }


def _common_eol(s: Session) -> list[dict]:
    """Packages past their end of life in the latest scan of branches in at
    least COMMON_MIN_WORKSPACES workspaces. With fewer, a name could point at
    one workspace, so it isn't shown."""
    latest = (select(func.max(SnapshotRow.id)).where(SnapshotRow.indexed.is_(True))
              .group_by(SnapshotRow.repo_id, SnapshotRow.branch))
    rows = s.execute(
        select(SnapshotRow.repo_id, EnrichmentRow.ecosystem, EnrichmentRow.name)
        .select_from(SnapshotRow)
        .join(FlagRow, FlagRow.snapshot_id == SnapshotRow.id)
        .join(EnrichmentRow, (EnrichmentRow.snapshot_id == FlagRow.snapshot_id)
              & (EnrichmentRow.key == FlagRow.key))
        .where(SnapshotRow.id.in_(latest), FlagRow.status.in_(EOL_FLAGS))).all()
    workspace_of = dict(s.execute(select(RepoRow.id, RepoRow.workspace_id)).all())
    found: dict[tuple[str, str], dict] = {}
    for repo, eco, name in rows:
        ws = workspace_of.get(repo)
        if ws is None:
            continue  # the history of a deleted repo
        entry = found.setdefault((eco, name.lower()), {"name": name, "workspaces": set(), "repos": set()})
        entry["workspaces"].add(ws)
        entry["repos"].add(repo)
    common = [{"ecosystem": eco, "name": e["name"], "workspaces": len(e["workspaces"]), "repos": len(e["repos"])}
              for (eco, _), e in found.items() if len(e["workspaces"]) >= COMMON_MIN_WORKSPACES]
    common.sort(key=lambda c: (-c["workspaces"], -c["repos"], c["name"].lower()))
    return common[:COMMON_LIMIT]


AI_STATUSES = ("ok", "partial", "quota", "failed", "no_key", "off")


def _ai_error_kind(detail: str | None) -> str:
    for kind, wording in AI_ERROR_TEXT.items():
        if detail and wording in detail:
            return kind
    return "other"


def ai(r: Range) -> dict:
    """AI research: how each scan's AI pass went, the providers used, tokens,
    searches and estimated spend per day, why passes failed, and what the
    research made of the packages it looked at."""
    with session() as s:
        rows = _runs(s, r.start, r.end, ScanRunRow.started_at, ScanRunRow.ai_status, ScanRunRow.ai_detail,
                     ScanRunRow.ai_provider, ScanRunRow.ai_model, ScanRunRow.ai_batches,
                     ScanRunRow.ai_tokens_in, ScanRunRow.ai_tokens_out, ScanRunRow.ai_searches,
                     ScanRunRow.ai_cost_usd)
        results = _ai_results(s, r)

    def bucket() -> dict:
        return {"runs": 0, "tokens_in": 0, "tokens_out": 0, "searches": 0, "cost_usd": 0.0}

    statuses: Counter = Counter()
    errors: Counter = Counter()
    providers: dict[str, dict] = {}
    models: dict[str, Counter] = defaultdict(Counter)
    unpriced: Counter = Counter()
    per_day: dict[date, dict] = defaultdict(bucket)
    totals = bucket()
    for started, status, detail, provider, model, batches, t_in, t_out, searches, cost in rows:
        status = status or "off"
        statuses[status] += 1
        if status in ("partial", "failed", "quota"):
            errors[_ai_error_kind(detail)] += 1
        if not batches:
            continue  # no AI call was made
        name = provider or "unknown"
        for b in (per_day[_day(started)], providers.setdefault(name, bucket()), totals):
            b["runs"] += 1
            b["tokens_in"] += t_in or 0
            b["tokens_out"] += t_out or 0
            b["searches"] += searches or 0
            b["cost_usd"] += cost or 0.0
        if cost is None:  # a model without a known price: left out of the spend
            unpriced[name] += 1
        if model:
            models[name][model] += 1
    status_order = list(AI_STATUSES) + sorted(set(statuses) - set(AI_STATUSES))
    return {
        "totals": {**totals, "cost_usd": round(totals["cost_usd"], 4), "scans": len(rows),
                   "unpriced_runs": sum(unpriced.values())},
        "statuses": [{"status": st, "runs": statuses[st]} for st in status_order if statuses.get(st)],
        "providers": sorted(({"provider": name, **b, "cost_usd": round(b["cost_usd"], 4),
                              "unpriced_runs": unpriced.get(name, 0),
                              "models": [m for m, _ in models[name].most_common()]}
                             for name, b in providers.items()), key=lambda p: (-p["runs"], p["provider"])),
        "days": [{"day": d.isoformat(), **per_day[d], "cost_usd": round(per_day[d]["cost_usd"], 4)}
                 for d in r.each_day()],
        "errors": [{"kind": k, "runs": n} for k, n in errors.most_common()],
        "results": results,
    }


def _ai_results(s: Session, r: Range) -> list[dict]:
    """What the AI research made of the packages it looked at, in the scans
    from the range that are still kept (old snapshots are pruned)."""
    verdict = EnrichmentRow.data[("ai_result", "verdict")].as_string()
    reused = EnrichmentRow.data["ai_reused_from"].as_string()
    how = case((reused.is_(None), "researched"), else_="reused")
    rows = s.execute(
        select(verdict, how, func.count())
        .select_from(EnrichmentRow)
        .join(SnapshotRow, SnapshotRow.id == EnrichmentRow.snapshot_id)
        .where(SnapshotRow.created_at >= r.lo, SnapshotRow.created_at < r.hi, verdict.is_not(None))
        .group_by(verdict, how)).all()
    return [
        {"key": "answered", "label": "Answered",
         "packages": sum(n for v, h, n in rows if h == "researched" and v != "unknown")},
        {"key": "unknown", "label": "Couldn't tell",
         "packages": sum(n for v, h, n in rows if h == "researched" and v == "unknown")},
        {"key": "reused", "label": "Reused a recent answer",
         "packages": sum(n for _, h, n in rows if h == "reused")},
    ]


TRIAL_LIMITS = ("trial_scans_per_day", "trial_scans_per_ip")


def trials(r: Range) -> dict:
    """Free scans without an account (trial.py): how many started, how they
    went, how many visitors signed in to keep theirs, and the ones refused (a
    limit, or a busy queue). From usage events, which outlast the guests."""
    with session() as s:
        rows = s.execute(select(UsageEventRow.event, UsageEventRow.at, UsageEventRow.meta).where(
            UsageEventRow.event.in_(("trial_scan", "trial_finished", "trial_saved", "trial_busy", "limit_hit")),
            UsageEventRow.at >= r.lo, UsageEventRow.at < r.hi)).all()
    per_day: dict[date, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    refused: Counter = Counter()
    for event, at, meta in rows:
        meta = meta or {}
        if event == "limit_hit":
            if meta.get("limit") in TRIAL_LIMITS:
                refused[meta["limit"]] += 1
            continue
        if event == "trial_busy":
            refused["busy"] += 1
            continue
        key = ({"trial_scan": "started", "trial_saved": "saved"}.get(event)
               or ("failed" if meta.get("outcome") == "error" else "results"))
        totals[key] += 1
        per_day[_day(at)][key] += 1
    return {
        "totals": {"started": totals["started"], "results": totals["results"], "failed": totals["failed"],
                   "saved": totals["saved"], "saved_pct": _pct(totals["saved"], totals["started"])},
        "days": [{"day": d.isoformat(), "started": per_day[d]["started"], "saved": per_day[d]["saved"]}
                 for d in r.each_day()],
        "refused": [{"reason": k, "count": n} for k, n in refused.most_common()],
    }


def workspaces(r: Range) -> dict:
    """One row per workspace: metadata and counts, nothing from its scans.
    Free scans' short-lived workspaces are left out (see `trials`)."""
    with session() as s:
        rows = s.execute(select(WorkspaceRow.id, WorkspaceRow.name, WorkspaceRow.personal,
                                WorkspaceRow.created_at).where(WorkspaceRow.trial.is_(False))).all()
        members: dict[str, set[str]] = defaultdict(set)
        for ws, uid in s.execute(select(MembershipRow.workspace_id, MembershipRow.user_id)):
            members[ws].add(uid)
        repos = Counter(s.scalars(select(RepoRow.workspace_id)))
        runs = _runs(s, r.start, r.end, ScanRunRow.workspace_id, ScanRunRow.outcome, ScanRunRow.ai_cost_usd)
        last = dict(s.execute(select(ScanRunRow.workspace_id, func.max(ScanRunRow.started_at))
                              .group_by(ScanRunRow.workspace_id)).all())
        active = set(s.scalars(select(UserActivityDayRow.user_id).distinct().where(
            UserActivityDayRow.day >= r.start, UserActivityDayRow.day <= r.end)))
        paused = {ws for ws, value in s.execute(
            select(WorkspaceSettingRow.workspace_id, WorkspaceSettingRow.value)
            .where(WorkspaceSettingRow.key == "scans_paused_at")) if value}
    scanned: Counter = Counter()
    failed: Counter = Counter()
    spend: dict[str, float] = defaultdict(float)
    for ws, outcome, cost in runs:
        scanned[ws] += 1
        if outcome == "error":
            failed[ws] += 1
        spend[ws] += cost or 0.0
    out = [{
        "id": ws, "name": name, "personal": bool(personal), "created_at": _iso(created),
        "members": len(members[ws]), "active_members": len(members[ws] & active),
        "repos": repos.get(ws, 0), "scans": scanned.get(ws, 0), "failed_scans": failed.get(ws, 0),
        "ai_cost_usd": round(spend.get(ws, 0.0), 4), "last_scan_at": _iso(last.get(ws)),
        "paused": ws in paused,
    } for ws, name, personal, created in rows]
    out.sort(key=lambda w: (-w["scans"], w["name"].lower()))
    return {"workspaces": out}


# --------------------------------------------------------------------------- #
# System health                                                                #
# --------------------------------------------------------------------------- #
SAMPLE_FIELDS = ("disk_used_bytes", "disk_total_bytes", "db_bytes", "uploads_bytes", "rss_bytes",
                 "mem_total_bytes", "queue_waiting", "scans_running")


def _nearest_dir(path: Path) -> Path:
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _proc_bytes(path: str, field: str) -> int | None:
    """A "<field>: <n> kB" line from /proc, in bytes (Linux); None elsewhere."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(field + ":"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _db_bytes(s: Session) -> int | None:
    try:
        if is_postgres():
            return int(s.execute(text("select pg_database_size(current_database())")).scalar_one())
        pages = s.execute(text("pragma page_count")).scalar_one()
        size = s.execute(text("pragma page_size")).scalar_one()
        return int(pages) * int(size)
    except Exception:  # noqa: BLE001 - a reading we can't take stays empty
        log.debug("could not read the database size", exc_info=True)
        return None


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def measure() -> dict:
    """The server's health right now. None for what this platform can't
    measure (memory is read from /proc, so only on Linux)."""
    upload_dir = Path(get_settings().upload_dir)
    try:
        disk = shutil.disk_usage(_nearest_dir(upload_dir))
        used, total = disk.used, disk.total
    except OSError:
        used = total = None
    waiting, running = scan_queue.stats()
    with session() as s:
        db_bytes = _db_bytes(s)
    return {
        "disk_used_bytes": used, "disk_total_bytes": total, "db_bytes": db_bytes,
        "uploads_bytes": _dir_bytes(upload_dir), "rss_bytes": _proc_bytes("/proc/self/status", "VmRSS"),
        "mem_total_bytes": _proc_bytes("/proc/meminfo", "MemTotal"),
        "queue_waiting": waiting, "scans_running": running,
    }


def record_sample() -> None:
    """Hourly (scheduler.py): store a reading, and drop the ones older than
    SAMPLE_KEEP_DAYS. Never raises."""
    try:
        reading = measure()
        with session() as s:
            s.add(SystemSampleRow(at=_now(), **reading))
            s.execute(delete(SystemSampleRow).where(
                SystemSampleRow.at < _now() - timedelta(days=SAMPLE_KEEP_DAYS)))
    except Exception:  # noqa: BLE001 - analytics must never break the app
        log.exception("could not record a system sample")


def system(r: Range) -> dict:
    """Now, plus the readings in the range: hourly, or each day's highest for
    ranges over a week."""
    with session() as s:
        rows = s.execute(select(SystemSampleRow.at, *(getattr(SystemSampleRow, f) for f in SAMPLE_FIELDS))
                         .where(SystemSampleRow.at >= r.lo, SystemSampleRow.at < r.hi)
                         .order_by(SystemSampleRow.at)).all()
    daily = r.days > 7
    points: dict[str, dict] = {}
    for at, *values in rows:
        key = _day(at).isoformat() if daily else _iso(at)
        point = points.setdefault(key, {"at": key, **{f: None for f in SAMPLE_FIELDS}})
        for f, v in zip(SAMPLE_FIELDS, values):
            if v is not None and (point[f] is None or v > point[f]):
                point[f] = v
    s = get_settings()
    # the public mode has one more worker, for free scans (scan_queue.trial)
    workers = s.scan_workers + (1 if s.public_mode() else 0)
    return {"now": measure(), "workers": workers, "per": "day" if daily else "hour",
            "samples": list(points.values())}


# --------------------------------------------------------------------------- #
# The panels, cached                                                           #
# --------------------------------------------------------------------------- #
PANELS: dict[str, Callable[[Range], dict]] = {
    "overview": overview, "growth": growth, "activation": activation, "retention": retention,
    "scans": scans, "content": content, "ai": ai, "system": system, "workspaces": workspaces,
    "trials": trials,
}
_LIVE = {"system"}  # measured fresh on every request

_cache: dict[tuple[str, date, date], tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def panel(name: str, r: Range) -> dict:
    """A panel's numbers for the range, cached for CACHE_SECONDS."""
    key = (name, r.start, r.end)
    if name not in _LIVE:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    result = {"range": r.as_dict(), "generated_at": _now().isoformat(), **PANELS[name](r)}
    if name not in _LIVE:
        with _cache_lock:
            if len(_cache) > 500:
                _cache.clear()
            _cache[key] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
