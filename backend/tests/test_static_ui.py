"""Phase 5: the image serves the built UI from the API's own address. Files as
they are, index.html for client-side routes, and the API's 404s untouched."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import serve_ui


def _dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root></div>")
    (root / "assets" / "app-abc123.js").write_text("console.log(1)")
    (root / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("not for the web")
    return root


def _app(root) -> TestClient:
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    serve_ui(app, root)
    return TestClient(app)


def test_client_side_routes_get_the_app(tmp_path):
    c = _app(_dist(tmp_path))
    for path in ("/", "/repos/r_1", "/settings", "/invite/tok"):
        r = c.get(path)
        assert r.status_code == 200 and "id=root" in r.text, path
        assert r.headers["cache-control"] == "no-cache"


def test_files_are_served_as_they_are(tmp_path):
    c = _app(_dist(tmp_path))
    assert c.get("/assets/app-abc123.js").text == "console.log(1)"
    assert c.get("/favicon.svg").text == "<svg/>"


def test_the_api_keeps_its_own_404s(tmp_path):
    c = _app(_dist(tmp_path))
    assert c.get("/api/health").json() == {"status": "ok"}
    r = c.get("/api/nope")
    assert r.status_code == 404 and "id=root" not in r.text


def test_nothing_outside_the_ui_folder_is_served(tmp_path):
    c = _app(_dist(tmp_path))
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        assert "not for the web" not in c.get(path).text, path


def test_no_index_html_means_no_ui(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _app(empty).get("/").status_code == 404
