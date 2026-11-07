"""Database-backed storage: behaviour the file version had implicitly (order,
duplicate keys, unlisted snapshots) plus migrations and the legacy importer."""
import datetime as dt
import io
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app import db, import_files, settings_store, storage
from app.db_models import Base
from app.models import (
    Ecosystem,
    Enrichment,
    Flag,
    FlagStatus,
    LogEntry,
    Package,
    Repo,
    ScanLog,
    SnapshotSummary,
    Source,
    Verdict,
)

TS1, TS2 = "2026-07-13T10-00-00Z", "2026-07-14T10-00-00Z"


def _pkg(name, version="1.0", source="requirements.txt"):
    return Package(name=name, version=version, ecosystem=Ecosystem.pypi, source_file=source)


def test_payloads_round_trip_in_order_with_duplicate_keys():
    # same package pinned in two manifests → two rows with one key; order kept
    pkgs = [_pkg("zeta"), _pkg("alpha"), _pkg("zeta", source="dev-requirements.txt")]
    storage.write_packages("r", "main", TS1, pkgs)
    assert storage.read_packages("r", "main", TS1) == pkgs

    e = Enrichment(name="zeta", version="1.0", ecosystem=Ecosystem.pypi, verdict=Verdict.eol,
                   eol_date=dt.date(2025, 1, 1), sources=[Source(url="https://x", note="n")],
                   security_supported=False, security_policy_row="< 2")
    storage.write_enrichment("r", "main", TS1, [e])
    assert storage.read_enrichment("r", "main", TS1) == [e]

    fl = [Flag(key="pypi:zeta:1.0", status=FlagStatus.ALREADY_EOL, reasons=["a", "b"]),
          Flag(key="pypi:alpha:1.0", status=FlagStatus.OK, reasons=["supported"]),
          Flag(key="pypi:zeta:1.0", status=FlagStatus.ALREADY_EOL, reasons=["a", "b"])]
    storage.write_flags("r", "main", TS1, fl)
    assert storage.read_flags("r", "main", TS1) == fl

    # a rewrite (re-flag) replaces, never appends
    storage.write_flags("r", "main", TS1, fl[:1])
    assert storage.read_flags("r", "main", TS1) == fl[:1]


def test_reads_of_missing_snapshot_are_empty():
    assert storage.read_packages("nope", "main", TS1) == []
    assert storage.read_enrichment("nope", "main", TS1) == []
    assert storage.read_flags("nope", "main", TS1) == []
    assert storage.read_scan_log("nope", "main", TS1) is None
    assert storage.read_report_csv("nope", "main", TS1) is None


def test_failed_scan_log_is_kept_but_not_listed():
    log = ScanLog(repo_id="r", branch="main", snapshot_ts=TS1,
                  entries=[LogEntry(ts=dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc),
                                    stage="scan", message="Scan failed: boom", level="error")])
    storage.write_scan_log("r", "main", TS1, log)
    assert storage.read_scan_log("r", "main", TS1) == log.model_dump(mode="json")
    assert storage.load_index("r", "main") == []
    assert storage.latest_snapshot_ts("r", "main") is None
    assert storage.read_report_csv("r", "main", TS1) is None   # not a listed snapshot


def test_save_index_makes_index_exactly_the_given_list():
    storage.upsert_index_entry("r", "main", SnapshotSummary(ts=TS1, package_count=1))
    storage.upsert_index_entry("r", "main", SnapshotSummary(ts=TS2, package_count=2))
    storage.write_packages("r", "main", TS1, [_pkg("a")])
    storage.save_index("r", "main", [SnapshotSummary(ts=TS2, package_count=5,
                                                     flag_counts={"OK": 5}, degraded=True)])
    assert storage.load_index("r", "main") == [
        SnapshotSummary(ts=TS2, package_count=5, flag_counts={"OK": 5}, degraded=True)]
    assert storage.read_packages("r", "main", TS1) == [_pkg("a")]  # payload kept, just unlisted


def test_report_csv_built_from_current_rows():
    storage.write_packages("r", "main", TS1, [_pkg("a")])
    storage.write_flags("r", "main", TS1, [Flag(key="pypi:a:1.0", status=FlagStatus.STALE)])
    storage.upsert_index_entry("r", "main", SnapshotSummary(ts=TS1, package_count=1))
    rows = storage.read_report_csv("r", "main", TS1).splitlines()
    assert rows[0].startswith("name,ecosystem,version")
    assert rows[1].startswith("a,pypi,1.0") and ",STALE," in rows[1]
    storage.write_flags("r", "main", TS1, [Flag(key="pypi:a:1.0", status=FlagStatus.OK)])
    assert ",OK," in storage.read_report_csv("r", "main", TS1).splitlines()[1]


def test_repo_order_branch_order_and_timestamps_round_trip():
    created = dt.datetime(2026, 7, 1, 9, 30, tzinfo=dt.timezone.utc)
    scanned = dt.datetime(2026, 7, 2, 6, 0, tzinfo=dt.timezone.utc)
    a = Repo(id="b-repo", url="https://x/b", branches=["release/6.1", "develop"],
             created_at=created, last_scan_by_branch={"develop": scanned, "removed": scanned},
             release_date=dt.date(2026, 9, 15), interval_days=7, token_override="t",
             inspect_image="img:1", detected_tag_vars=["CORE_TAG"])
    b = Repo(id="a-repo", url="https://x/a", branches=["main"])
    storage.upsert_repo(a)
    storage.upsert_repo(b)
    assert [r.id for r in storage.load_repos()] == ["b-repo", "a-repo"]  # insertion order
    assert storage.get_repo("b-repo") == a
    # update keeps position, replaces branches
    a.branches = ["develop"]
    storage.upsert_repo(a)
    assert [r.id for r in storage.load_repos()] == ["b-repo", "a-repo"]
    assert storage.get_repo("b-repo").branches == ["develop"]
    storage.save_repos([b])
    assert [r.id for r in storage.load_repos()] == ["a-repo"]


def test_runtime_settings_persist():
    assert settings_store.update_runtime_settings(
        {"use_enrichment_agent": False, "bogus": True})["use_enrichment_agent"] is False
    assert settings_store.get_runtime_settings()["use_enrichment_agent"] is False
    assert "bogus" not in settings_store.get_runtime_settings()


# SQLite can't reflect the lower(name) expression indexes; Postgres compares
# them fully (`alembic check` against the real DB).
@pytest.mark.filterwarnings("ignore:.*expression-based index")
def test_alembic_migrations_match_models(tmp_path, monkeypatch):
    """`alembic upgrade head` builds exactly the schema the ORM declares."""
    url = f"sqlite:///{tmp_path}/mig.db"
    monkeypatch.setenv("DATABASE_URL", url)
    from app.config import get_settings
    get_settings.cache_clear()
    db.reset_engine()
    command.upgrade(db.alembic_config(), "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
    command.downgrade(db.alembic_config(), "base")


# --------------------------------------------------------------------------- #
# legacy importer                                                              #
# --------------------------------------------------------------------------- #
def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, default=str))


def _legacy_tree(root: Path) -> None:
    _write(root / "repos.json", [
        {"id": "multi", "url": "https://x/multi", "branches": ["release/6.1", "develop"]},
        {"id": "flat", "url": "https://x/flat", "branch": "develop-7.0.0"},  # pre-multi-branch
    ])
    _write(root / "app_settings.json", {"use_enrichment_agent": False})
    b = root / "snapshots" / "multi" / "release_6.1"
    _write(b / "index.json", [{"ts": TS2, "package_count": 1, "flag_counts": {"OK": 1}, "degraded": False}])
    _write(b / TS2 / "packages.json", [_pkg("a").model_dump(mode="json")])
    _write(b / TS2 / "flags.json", [{"key": "pypi:a:1.0", "status": "OK", "reasons": []}])
    _write(b / TS2 / "enrichment.json", [])
    _write(b / TS2 / "scan_log.json", {"repo_id": "multi", "branch": "release/6.1", "entries": []})
    _write(b / TS1 / "scan_log.json", {"repo_id": "multi", "branch": "release/6.1", "entries": []})  # failed scan
    f = root / "snapshots" / "flat"
    _write(f / "index.json", [{"ts": TS1, "package_count": 0, "flag_counts": {}, "degraded": True}])
    _write(f / TS1 / "packages.json", [])
    # history of a repo that is no longer registered, branch name only in its scan log
    o = root / "snapshots" / "orphan" / "feature_x"
    _write(o / "index.json", [{"ts": TS1, "package_count": 0, "flag_counts": {}, "degraded": False}])
    _write(o / TS1 / "scan_log.json", {"repo_id": "orphan", "branch": "feature/x", "entries": []})


def test_importer_imports_everything_once(tmp_path):
    _legacy_tree(tmp_path)
    before = sorted(p.as_posix() for p in tmp_path.rglob("*"))
    c = import_files.run(tmp_path, out=io.StringIO())
    assert (c.repos_added, c.settings_added, c.snapshots_added, c.packages) == (2, 1, 4, 1)

    assert [r.id for r in storage.load_repos()] == ["multi", "flat"]
    assert storage.get_repo("flat").branches == ["develop-7.0.0"]
    assert settings_store.get_runtime_settings()["use_enrichment_agent"] is False

    # slug → real branch name; failed scan's log kept but unlisted
    assert [s.ts for s in storage.load_index("multi", "release/6.1")] == [TS2]
    assert storage.read_packages("multi", "release/6.1", TS2) == [_pkg("a")]
    assert storage.read_scan_log("multi", "release/6.1", TS1) is not None
    # flat layout → first branch
    assert storage.load_index("flat", "develop-7.0.0")[0].degraded is True
    # orphan history: branch name recovered from its scan log
    assert storage.latest_snapshot_ts("orphan", "feature/x") == TS1

    # second run is a no-op; files never touched
    c2 = import_files.run(tmp_path, out=io.StringIO())
    assert (c2.repos_added, c2.repos_skipped, c2.snapshots_added, c2.snapshots_skipped) == (0, 2, 0, 4)
    assert sorted(p.as_posix() for p in tmp_path.rglob("*")) == before


def test_importer_dry_run_writes_nothing(tmp_path):
    _legacy_tree(tmp_path)
    c = import_files.run(tmp_path, dry_run=True, out=io.StringIO())
    assert c.snapshots_added == 4
    assert storage.load_repos() == []
    assert storage.load_index("multi", "release/6.1") == []
    assert settings_store.get_runtime_settings()["use_enrichment_agent"] is True


def test_report_csv_is_served_with_lf_like_before():
    storage.write_packages("r", "main", TS1, [_pkg("a")])
    storage.upsert_index_entry("r", "main", SnapshotSummary(ts=TS1, package_count=1))
    text = storage.read_report_csv("r", "main", TS1)
    assert "\r" not in text and text.count("\n") == 2


def test_nul_characters_are_dropped_not_fatal():
    e = Enrichment(name="x", version="1", ecosystem=Ecosystem.npm, summary="bad\x00msg",
                   sources=[Source(url="https://x", note="n\x00")])
    storage.write_enrichment("r", "main", TS1, [e])
    got = storage.read_enrichment("r", "main", TS1)[0]
    assert got.summary == "badmsg" and got.sources[0].note == "n"
    storage.write_packages("r", "main", TS1, [_pkg("we\x00ird")])
    assert storage.read_packages("r", "main", TS1)[0].name == "weird"


def test_settings_upsert_twice():
    settings_store.update_runtime_settings({"use_enrichment_agent": False})
    settings_store.update_runtime_settings({"use_enrichment_agent": True})
    assert settings_store.get_runtime_settings()["use_enrichment_agent"] is True


def test_dry_run_cli_does_not_create_schema(tmp_path, monkeypatch):
    _legacy_tree(tmp_path / "legacy")
    url = f"sqlite:///{tmp_path}/fresh.db"
    monkeypatch.setenv("DATABASE_URL", url)
    from app.config import get_settings
    from sqlalchemy import inspect
    get_settings.cache_clear()
    db.reset_engine()
    assert import_files.main(["--data-dir", str(tmp_path / "legacy"), "--dry-run"]) == 0
    assert inspect(create_engine(url)).get_table_names() == []
