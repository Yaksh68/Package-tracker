"""Repos scanned from an uploaded zip instead of a git clone."""
import io
import json
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import pipeline, scheduler, storage, uploads
from app.agents.clone import CloneError
from app.config import get_settings
from app.main import app
from app.models import ScanState


def _zip(files: dict[str, str], symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


PROJECT = _zip({
    "app-ui/requirements.txt": "requests==2.32.4\n",
    "app-ui/node_modules/left-pad/package.json": "{}",
    "app-ui/.git/HEAD": "ref: refs/heads/main",
})


# --------------------------------------------------------------------------- #
# storing + unpacking                                                          #
# --------------------------------------------------------------------------- #
def test_unpack_roots_at_the_single_folder_and_skips_unscanned_dirs():
    uploads.save("ui", "6.1.0", io.BytesIO(PROJECT))
    with uploads.unpack("ui", "6.1.0") as clone:
        assert clone.path.name == "app-ui"
        assert (clone.path / "requirements.txt").read_text() == "requests==2.32.4\n"
        assert not (clone.path / "node_modules").exists()
        assert not (clone.path / ".git").exists()


def test_symlinks_are_not_unpacked():
    uploads.save("r", "v", io.BytesIO(_zip({"a/req.txt": "x"}, symlinks={"a/evil": "/etc/passwd"})))
    with uploads.unpack("r", "v") as clone:
        assert not (clone.path / "evil").exists()


@pytest.mark.parametrize("data, msg", [
    (b"not a zip at all", "not a .zip"),
    (_zip({"../escape.txt": "x"}), "outside the archive"),
    (_zip({"/etc/cron.d/x": "x"}), "outside the archive"),
    (_zip({"node_modules/a.js": "x"}), "no files to scan"),
])
def test_bad_archives_are_rejected(data, msg):
    with pytest.raises(uploads.UploadError, match=msg):
        uploads.save("r", "v", io.BytesIO(data))
    assert not uploads.has_upload("r", "v")


def test_limits(monkeypatch):
    monkeypatch.setenv("UPLOAD_MAX_FILES", "2")
    get_settings.cache_clear()
    with pytest.raises(uploads.UploadError, match="limit is 2"):
        uploads.save("r", "v", io.BytesIO(_zip({"a": "1", "b": "2", "c": "3"})))
    monkeypatch.setenv("UPLOAD_MAX_FILES", "100")
    monkeypatch.setenv("UPLOAD_MAX_UNPACKED_MB", "1")
    get_settings.cache_clear()
    with pytest.raises(uploads.UploadError, match="unpacks to"):
        uploads.save("r", "v", io.BytesIO(_zip({"big.txt": "x" * (2 * 1024 * 1024)})))


def test_versions_are_stored_separately_and_missing_one_is_a_clone_error():
    uploads.save("r", "release/6.1", io.BytesIO(_zip({"a.txt": "6.1"})))
    uploads.save("r", "release 6.1", io.BytesIO(_zip({"a.txt": "other"})))
    with uploads.unpack("r", "release/6.1") as c:
        assert (c.path / "a.txt").read_text() == "6.1"
    with pytest.raises(CloneError, match="no zip uploaded"):
        uploads.unpack("r", "7.0")


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    queued = []
    monkeypatch.setattr(scheduler, "trigger_now", lambda repo, branch, **kw: queued.append((repo.id, branch)))
    c = TestClient(app)
    c.queued = queued
    return c


def _create(client, data=PROJECT, filename="app-ui.zip", **meta):
    meta = {"name": "App UI", **meta}
    return client.post("/api/repos/upload", data={"meta": json.dumps(meta)},
                       files={"file": (filename, data, "application/zip")})


def test_create_upload_repo_and_scan_it(client):
    r = _create(client, version="6.1.0", ignored_packages=["enzyme"])
    assert r.status_code == 201, r.text
    repo = r.json()
    assert (repo["id"], repo["source"], repo["url"], repo["branches"]) == \
        ("app-ui", "upload", "upload://app-ui.zip", ["6.1.0"])
    assert repo["ignored_packages"] == ["enzyme"]
    assert client.queued == [("app-ui", "6.1.0")]
    assert uploads.has_upload("app-ui", "6.1.0")


def test_version_is_optional(client):
    assert _create(client).json()["branches"] == ["upload"]


def test_rejected_zip_creates_nothing(client):
    r = _create(client, data=_zip({"../x": "1"}))
    assert r.status_code == 400 and "outside the archive" in r.json()["detail"]
    assert storage.get_repo("app-ui") is None and client.queued == []
    assert _create(client, filename="app-ui.tar.gz").status_code == 400


def test_upload_new_version_then_replace_it(client):
    _create(client, version="6.1.0")
    r = client.post("/api/repos/app-ui/upload", data={"version": "7.0.0"},
                    files={"file": ("app-ui-7.zip", _zip({"a.txt": "7"}), "application/zip")})
    assert r.json()["branches"] == ["6.1.0", "7.0.0"] and r.json()["url"] == "upload://app-ui-7.zip"
    client.post("/api/repos/app-ui/upload", data={"version": "7.0.0"},
                files={"file": ("app-ui-7b.zip", _zip({"a.txt": "7b"}), "application/zip")})
    assert storage.get_repo("app-ui").branches == ["6.1.0", "7.0.0"]
    with uploads.unpack("app-ui", "7.0.0") as c:
        assert (c.path / "a.txt").read_text() == "7b"
    assert client.queued[-2:] == [("app-ui", "7.0.0"), ("app-ui", "7.0.0")]


def test_git_repos_take_no_uploads(client):
    rid = client.post("/api/repos", json={"url": "https://gitlab.com/x/app.git", "branches": ["main"]}).json()["id"]
    r = client.post(f"/api/repos/{rid}/upload", files={"file": ("a.zip", PROJECT, "application/zip")})
    assert r.status_code == 400


def test_removing_a_version_or_the_repo_drops_its_zips(client):
    _create(client, version="6.1.0")
    client.post("/api/repos/app-ui/upload", data={"version": "7.0.0"},
                files={"file": ("b.zip", PROJECT, "application/zip")})
    client.put("/api/repos/app-ui", json={"branches": ["7.0.0"]})
    assert not uploads.has_upload("app-ui", "6.1.0") and uploads.has_upload("app-ui", "7.0.0")
    assert client.delete("/api/repos/app-ui").status_code == 204
    assert not uploads.has_upload("app-ui", "7.0.0")


def test_scan_now_needs_a_zip(client):
    _create(client, version="6.1.0")
    client.put("/api/repos/app-ui", json={"branches": ["6.1.0", "7.0.0"]})
    assert client.post("/api/repos/app-ui/scan", params={"branch": "7.0.0"}).status_code == 400
    assert client.post("/api/repos/app-ui/scan", params={"branch": "6.1.0"}).status_code == 202


# --------------------------------------------------------------------------- #
# pipeline                                                                     #
# --------------------------------------------------------------------------- #
async def test_scan_reads_the_upload_instead_of_cloning(monkeypatch):
    def no_clone(*a, **kw):
        raise AssertionError("upload repos must not be cloned")

    monkeypatch.setattr(pipeline, "shallow_clone", no_clone)
    uploads.save("app-ui", "6.1.0", io.BytesIO(_zip({"app-ui/README.md": "no manifests"})))
    from app.models import Repo
    repo = storage.upsert_repo(Repo(id="app-ui", source="upload", url="upload://app-ui.zip",
                                    branches=["6.1.0"]))
    status = await pipeline.run_scan(repo, "6.1.0", use_extraction_agent=False)
    assert status.state == ScanState.done and not status.degraded
    log = storage.read_scan_log("app-ui", "6.1.0", status.snapshot_ts)
    assert any("Unpacked uploaded zip for version 6.1.0" in e["message"] for e in log["entries"])
