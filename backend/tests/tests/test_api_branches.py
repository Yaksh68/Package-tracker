"""API-level coverage for per-branch tracking: a repo with several branches
scans/serves each branch independently, and the branch query param is required
and validated on every snapshot/scan endpoint."""
import pytest
from fastapi.testclient import TestClient

from app import scheduler, storage
from app.main import app


@pytest.fixture(autouse=True)
def _no_real_scans(monkeypatch):
    # create_repo / scan_now fire background scans via scheduler.trigger_now;
    # without this the test would spawn real asyncio tasks doing a real
    # `git clone` against a fake URL. sync_repo/unsync_repo are no-ops already
    # (no APScheduler instance running under a plain TestClient).
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch: None)


@pytest.fixture
def client():
    return TestClient(app)


def test_create_repo_reports_branches_info_per_branch(client):
    r = client.post("/api/repos", json={
        "url": "https://x/multi-branch", "branches": ["develop", "release/6.1"],
    })
    assert r.status_code == 201
    body = r.json()
    assert body["branches"] == ["develop", "release/6.1"]

    listed = next(x for x in client.get("/api/repos").json() if x["id"] == body["id"])
    branches = {b["branch"]: b for b in listed["branches_info"]}
    assert set(branches) == {"develop", "release/6.1"}
    assert branches["develop"]["latest_snapshot"] is None
    assert branches["develop"]["scan_state"] == "idle"


def test_snapshot_endpoints_require_a_valid_branch(client):
    repo_id = client.post("/api/repos", json={
        "url": "https://x/valid-branch", "branches": ["main"],
    }).json()["id"]

    # write a snapshot directly (bypassing the pipeline) to exercise reads
    storage.upsert_index_entry(repo_id, "main", storage.SnapshotSummary(ts="2026-07-20T10-00-00Z"))
    storage.write_packages(repo_id, "main", "2026-07-20T10-00-00Z", [])

    ok = client.get(f"/api/repos/{repo_id}/snapshots", params={"branch": "main"})
    assert ok.status_code == 200 and len(ok.json()) == 1

    missing_param = client.get(f"/api/repos/{repo_id}/snapshots")
    assert missing_param.status_code == 422  # branch is required, no default

    wrong_branch = client.get(f"/api/repos/{repo_id}/snapshots", params={"branch": "nope"})
    assert wrong_branch.status_code == 404


def test_scan_now_validates_branch_membership(client):
    repo_id = client.post("/api/repos", json={
        "url": "https://x/scan-branch", "branches": ["a", "b"],
    }).json()["id"]

    ok = client.post(f"/api/repos/{repo_id}/scan", params={"branch": "a"})
    assert ok.status_code == 202

    bad = client.post(f"/api/repos/{repo_id}/scan", params={"branch": "c"})
    assert bad.status_code == 404


def test_update_shrinking_branches_drops_stale_snapshot_visibility(client):
    repo_id = client.post("/api/repos", json={
        "url": "https://x/shrink-branch", "branches": ["keep", "drop"],
    }).json()["id"]

    r = client.put(f"/api/repos/{repo_id}", json={"branches": ["keep"]})
    assert r.status_code == 200
    assert r.json()["branches"] == ["keep"]

    # the dropped branch is no longer valid for this repo
    assert client.get(
        f"/api/repos/{repo_id}/snapshots", params={"branch": "drop"}
    ).status_code == 404


def test_delete_repo_removes_every_branch(client):
    repo_id = client.post("/api/repos", json={
        "url": "https://x/delete-branch", "branches": ["one", "two"],
    }).json()["id"]

    assert client.delete(f"/api/repos/{repo_id}").status_code == 204
    assert client.get(f"/api/repos/{repo_id}").status_code == 404


def test_create_repo_rejects_empty_branches(client):
    r = client.post("/api/repos", json={"url": "https://x/no-branches", "branches": []})
    assert r.status_code == 422
