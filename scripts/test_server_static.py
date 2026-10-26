"""The built frontend served by the API process (server_static, the installable release)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server_static


@pytest.fixture
def client(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "app-123.js").write_text("console.log(1)")
    (dist / "sw.js").write_text("self.x = 1")
    (tmp_path / "secret.txt").write_text("outside")
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}
    server_static.mount_frontend(app, str(dist))
    return TestClient(app)


def test_serves_files_and_the_app_shell_for_other_paths(client):
    assert client.get("/").text.startswith("<!doctype html>")
    js = client.get("/assets/app-123.js")
    assert js.text == "console.log(1)" and "immutable" in js.headers["cache-control"]
    assert client.get("/sw.js").headers["cache-control"] == "no-cache"
    # A path the browser routes: the shell.
    assert client.get("/books/123").text.startswith("<!doctype html>")


def test_api_routes_win_and_unknown_api_paths_stay_404(client):
    assert client.get("/api/ping").json() == {"ok": True}
    assert client.get("/api/nope").status_code == 404


def test_never_serves_outside_the_build_directory(client):
    res = client.get("/..%2Fsecret.txt")
    assert "outside" not in res.text


def test_refuses_to_start_without_a_build(tmp_path):
    with pytest.raises(SystemExit):
        server_static.mount_frontend(FastAPI(), str(tmp_path / "missing"))
