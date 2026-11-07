"""Per-repo ignore list: listed packages are flagged IGNORED (still shown, never
counted as risk)."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app import scheduler, storage
from app.flags import evaluate_all
from app.main import app
from app.models import (
    Ecosystem,
    Enrichment,
    FlagStatus,
    Package,
    Repo,
    SnapshotSummary,
    Verdict,
)

TS = "2026-10-12T10-00-00Z"


def _npm(name, version="1.0.0"):
    return Package(name=name, version=version, ecosystem=Ecosystem.npm, source_file="package.json")


def _eol(name):
    return Enrichment(name=name, version="1.0.0", ecosystem=Ecosystem.npm, verdict=Verdict.eol)


PKGS = [_npm("enzyme"), _npm("@cfaester/enzyme-adapter-react-18"), _npm("react")]
ENR = {p.key: _eol(p.name) for p in PKGS}


def test_ignored_names_get_ignored_flag():
    flags = evaluate_all(PKGS, ENR, None, ["Enzyme", "@cfaester/enzyme-adapter-react-18"],
                         today=dt.date(2026, 10, 12))
    assert [f.status for f in flags] == [FlagStatus.IGNORED, FlagStatus.IGNORED, FlagStatus.ALREADY_EOL]
    assert flags[0].reasons == ["on this repo's ignore list"]


def test_pypi_style_names_match_across_separators():
    p = Package(name="Foo_Bar", version="1", ecosystem=Ecosystem.pypi, source_file="requirements.txt")
    assert evaluate_all([p], {}, None, ["foo-bar"])[0].status == FlagStatus.IGNORED


def test_repo_cleans_the_list():
    r = Repo(id="r", url="u", branches=["m"], ignored_packages=[" enzyme ", "", "ENZYME", "jest"])
    assert r.ignored_packages == ["enzyme", "jest"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch: None)
    return TestClient(app)


def test_changing_the_list_reflags_the_latest_snapshot(client):
    rid = client.post("/api/repos", json={"url": "https://x/ui", "branches": ["main"]}).json()["id"]
    storage.upsert_index_entry(rid, "main", SnapshotSummary(ts=TS, package_count=3))
    storage.write_packages(rid, "main", TS, PKGS)
    storage.write_enrichment(rid, "main", TS, list(ENR.values()))
    storage.write_flags(rid, "main", TS, evaluate_all(PKGS, ENR, None))

    r = client.put(f"/api/repos/{rid}", json={"ignored_packages": ["enzyme", "@cfaester/enzyme-adapter-react-18"]})
    assert r.json()["ignored_packages"] == ["enzyme", "@cfaester/enzyme-adapter-react-18"]
    rows = client.get(f"/api/repos/{rid}/snapshots/{TS}/components", params={"branch": "main"}).json()
    assert {x["name"]: x["flag"] for x in rows} == {
        "enzyme": "IGNORED", "@cfaester/enzyme-adapter-react-18": "IGNORED", "react": "ALREADY_EOL"}
    assert storage.load_index(rid, "main")[0].flag_counts == {"IGNORED": 2, "ALREADY_EOL": 1}

    # emptying the list brings them back
    client.put(f"/api/repos/{rid}", json={"ignored_packages": []})
    assert storage.load_index(rid, "main")[0].flag_counts == {"ALREADY_EOL": 3}
