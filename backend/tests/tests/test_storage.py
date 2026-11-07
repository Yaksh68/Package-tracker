from datetime import date, datetime, timezone

from sqlalchemy import func, select

from app import db, storage
from app.db_models import FlagRow, PackageRow, SnapshotRow
from app.models import Ecosystem, Flag, FlagStatus, Package, Repo


def test_repo_crud_roundtrip():
    repo = Repo(id="app-core", url="https://gitlab.com/x/app-core", branches=["main"],
                release_date=date(2026, 9, 15), interval_days=1,
                created_at=datetime.now(timezone.utc))
    storage.upsert_repo(repo)
    assert storage.get_repo("app-core").branches == ["main"]
    assert len(storage.load_repos()) == 1
    assert storage.delete_repo("app-core") is True
    assert storage.get_repo("app-core") is None


def test_delete_repo_removes_all_snapshot_data():
    repo = Repo(id="gone", url="https://x/gone", branches=["main"])
    storage.upsert_repo(repo)
    ts = "2026-07-14T10-00-00Z"
    storage.upsert_index_entry("gone", "main", storage.SnapshotSummary(ts=ts))
    storage.write_packages("gone", "main", ts, [
        Package(name="a", version="1", ecosystem=Ecosystem.pypi, source_file="r.txt")])
    storage.write_flags("gone", "main", ts, [Flag(key="pypi:a:1", status=FlagStatus.OK)])

    assert storage.delete_repo("gone") is True
    assert storage.get_repo("gone") is None
    assert storage.load_index("gone", "main") == []   # nothing lingers for a re-add
    assert storage.read_packages("gone", "main", ts) == []
    with db.session() as s:                            # children cascaded too
        for model in (SnapshotRow, PackageRow, FlagRow):
            assert s.scalar(select(func.count()).select_from(model)) == 0
    assert storage.delete_repo("gone") is False


def test_legacy_interval_hours_migrates_to_days():
    # an old repos.json entry stored interval_hours; it must load as interval_days
    r = Repo(id="old", url="https://x/old", branches=["main"], interval_hours=48)
    assert r.interval_days == 2
    r2 = Repo(id="old2", url="https://x/old2", branches=["main"], interval_hours=24)
    assert r2.interval_days == 1


def test_legacy_single_branch_migrates_to_branches_list():
    # an old repos.json entry stored a single `branch` string
    r = Repo(**{"id": "old", "url": "https://x/old", "branch": "develop-7.0.0"})
    assert r.branches == ["develop-7.0.0"]


def test_legacy_last_scan_migrates_to_last_scan_by_branch():
    ts = datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc)
    r = Repo(**{"id": "old", "url": "https://x/old", "branch": "develop-7.0.0", "last_scan": ts})
    assert r.last_scan_by_branch == {"develop-7.0.0": ts}


def test_branches_require_at_least_one_and_dedupe():
    r = Repo(id="x", url="https://x/x", branches=[" main ", "main", "release/6.1", ""])
    assert r.branches == ["main", "release/6.1"]
    for empty in ([], [""], ["  "]):
        try:
            Repo(id="y", url="https://x/y", branches=empty)
        except Exception:
            continue
        raise AssertionError(f"accepted empty branches: {empty}")


def test_image_tags_are_validated_and_persist():
    repo = Repo(id="compose", url="https://x/compose", branches=["main"], image_tags={
        " CORE_TAG ": " acme/platform-dev:core-latest ",   # trimmed
        "UI_TAG": "",                                          # no value → dropped
        "MONGO_TAG": None,                                     # ditto
    })
    assert repo.image_tags == {"CORE_TAG": "acme/platform-dev:core-latest"}
    storage.upsert_repo(repo)
    assert storage.get_repo("compose").image_tags == repo.image_tags
    storage.delete_repo("compose")


def test_image_tags_reject_injection_and_bad_names():
    for bad in ({"CORE_TAG": "img; rm -rf /"}, {"CORE_TAG": "--privileged"},
                {"CORE_TAG": "img with spaces"}, {"BAD-NAME": "redis:7"}):
        try:
            Repo(id="x", url="https://x/x", branches=["main"], image_tags=bad)
        except Exception:
            continue
        raise AssertionError(f"accepted invalid image_tags: {bad}")


def test_slugify_repo_id():
    assert storage.slugify_repo_id("https://gitlab.com/acme/My-Service.git") == "my-service"


def test_snapshot_index_newest_first():
    storage.upsert_index_entry("r", "main", storage.SnapshotSummary(ts="2026-07-13T10-00-00Z"))
    storage.upsert_index_entry("r", "main", storage.SnapshotSummary(ts="2026-07-14T10-00-00Z"))
    idx = storage.load_index("r", "main")
    assert idx[0].ts == "2026-07-14T10-00-00Z"
    assert storage.latest_snapshot_ts("r", "main") == "2026-07-14T10-00-00Z"


def test_snapshot_index_is_independent_per_branch():
    storage.upsert_index_entry("r2", "main", storage.SnapshotSummary(ts="2026-07-13T10-00-00Z"))
    storage.upsert_index_entry("r2", "release/6.1", storage.SnapshotSummary(ts="2026-07-20T10-00-00Z"))
    assert storage.latest_snapshot_ts("r2", "main") == "2026-07-13T10-00-00Z"
    assert storage.latest_snapshot_ts("r2", "release/6.1") == "2026-07-20T10-00-00Z"
    # branch names with `/` get a filesystem-safe slug, not a nested dir
    assert storage.branch_dir_name("release/6.1") == "release_6.1"
