"""API-level coverage for the per-package "latest stable / upgrade available"
fields on the components endpoint."""
import pytest
from fastapi.testclient import TestClient

from app import scheduler, storage
from app.main import app
from app.models import Ecosystem, Enrichment, Package, Verdict

TS = "2026-07-20T10-00-00Z"


@pytest.fixture(autouse=True)
def _no_real_scans(monkeypatch):
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch, **kw: None)


@pytest.fixture
def client():
    return TestClient(app)


def _pkg(name, version, eco=Ecosystem.pypi, source="requirements.txt", direct=True):
    return Package(name=name, version=version, ecosystem=eco,
                   source_file=source, is_direct=direct)


def _enr(name, version, latest=None, stable=None, eco=Ecosystem.pypi):
    return Enrichment(name=name, version=version, ecosystem=eco,
                      verdict=Verdict.supported, latest_version=latest,
                      latest_stable_version=stable)


def _seed(repo_id, branch, packages, enrichments):
    storage.upsert_index_entry(repo_id, branch, storage.SnapshotSummary(ts=TS))
    storage.write_packages(repo_id, branch, TS, packages)
    storage.write_enrichment(repo_id, branch, TS, enrichments)
    storage.write_flags(repo_id, branch, TS, [])


def test_components_report_latest_stable_and_upgrade_available(client):
    repo_id = client.post("/api/repos", json={
        "url": "https://x/upgrades", "branches": ["main"],
    }).json()["id"]

    packages = [
        _pkg("behind", "1.0.0"),
        _pkg("current", "2.5.0"),
        # `latest` is a prerelease and there's no stable ahead → no upgrade claim
        _pkg("only-rc-ahead", "3.0.0"),
        # Debian-style apt version can't be compared → must stay quiet
        _pkg("python3.12", "3.12.3-1ubuntu0.15", eco=Ecosystem.apt,
             source="image:ubuntu:24.04", direct=False),
    ]
    enrichments = [
        _enr("behind", "1.0.0", latest="1.2.0", stable="1.2.0"),
        _enr("current", "2.5.0", latest="2.5.0", stable="2.5.0"),
        _enr("only-rc-ahead", "3.0.0", latest="4.0.0rc1", stable=None),
        _enr("python3.12", "3.12.3-1ubuntu0.15", latest="3.12.11",
             stable="3.12.11", eco=Ecosystem.apt),
    ]
    _seed(repo_id, "main", packages, enrichments)

    rows = client.get(f"/api/repos/{repo_id}/snapshots/{TS}/components",
                      params={"branch": "main"}).json()
    by_name = {r["name"]: r for r in rows}

    assert by_name["behind"]["latest_stable_version"] == "1.2.0"
    assert by_name["behind"]["update_available"] is True

    assert by_name["current"]["update_available"] is False

    # a prerelease `latest` is surfaced but never advertised as an upgrade
    assert by_name["only-rc-ahead"]["latest_version"] == "4.0.0rc1"
    assert by_name["only-rc-ahead"]["latest_stable_version"] is None
    assert by_name["only-rc-ahead"]["update_available"] is False

    assert by_name["python3.12"]["update_available"] is False


def test_report_csv_includes_the_new_columns(client):
    repo_id = client.post("/api/repos", json={
        "url": "https://x/upgrade-csv", "branches": ["main"],
    }).json()["id"]
    _seed(repo_id, "main",
          [_pkg("behind", "1.0.0")],
          [_enr("behind", "1.0.0", latest="1.2.0", stable="1.2.0")])
    # the CSV is written by the pipeline; build it the same way here
    packages = storage.read_packages(repo_id, "main", TS)
    enrich_by_key = {e.key: e for e in storage.read_enrichment(repo_id, "main", TS)}
    csv_text = storage.build_report_csv(packages, enrich_by_key, {})

    header, row = csv_text.splitlines()[0], csv_text.splitlines()[1]
    assert "latest_stable_version" in header and "update_available" in header
    assert "1.2.0" in row and "True" in row
