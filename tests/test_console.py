"""Tests for `vouch console` — serving the vendored React SPA + /proxy bridge.

The console is a static single-page app that reaches vouch HTTP backends
through a same-origin ``/proxy/*`` bridge (the browser can't call vouch
cross-origin — vouch deliberately sends no CORS headers). In dev that bridge
is a vite plugin (`webapp/plugins/vouch-proxy.ts`); these cover the Python
reimplementation of it plus the console-directory resolution that lets one
`pip install` serve the built SPA with no node.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("starlette", reason="vouch console needs the [web] extra")

from fastapi.testclient import TestClient

from vouch.web import console as console_mod
from vouch.web.console import build_console_app, resolve_console_dir

# --- a stub "vouch serve --transport http" upstream -------------------------


class _StubBackend(BaseHTTPRequestHandler):
    """Records what the proxy forwarded; echoes enough to assert on."""

    seen: ClassVar[list[dict[str, Any]]] = []

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        _StubBackend.seen.append(
            {"method": "GET", "path": self.path, "auth": self.headers.get("authorization")}
        )
        if self.path == "/boom":
            self._reply(401, {"ok": False, "error": {"code": "unauthorized"}})
            return
        self._reply(200, {"ok": True, "path": self.path})

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n).decode()
        _StubBackend.seen.append({"method": "POST", "path": self.path, "body": raw})
        self._reply(200, {"ok": True, "echo": raw})

    def log_message(self, *_a: Any) -> None:  # silence the test log
        pass


@pytest.fixture
def upstream():
    _StubBackend.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _StubBackend)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()


@pytest.fixture
def console_dir(tmp_path: Path) -> Path:
    d = tmp_path / "console"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(
        "<!doctype html><title>vouch console</title>", encoding="utf-8"
    )
    (d / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    return d


def _loopback_client(app) -> TestClient:  # type: ignore[no-untyped-def]
    """A same-origin loopback browser client."""
    return TestClient(app, client=("127.0.0.1", 54321))


# --- static SPA serving -----------------------------------------------------


def test_serves_the_spa_index_at_root(console_dir: Path) -> None:
    res = _loopback_client(build_console_app(console_dir)).get("/")
    assert res.status_code == 200
    assert "vouch console" in res.text


def test_unknown_route_falls_back_to_index_for_client_routing(console_dir: Path) -> None:
    # a SPA deep link the server has no file for must return index.html (200),
    # not 404 — client-side routing renders the view.
    res = _loopback_client(build_console_app(console_dir)).get("/review")
    assert res.status_code == 200
    assert "vouch console" in res.text


def test_real_static_asset_is_served(console_dir: Path) -> None:
    res = _loopback_client(build_console_app(console_dir)).get("/assets/app.js")
    assert res.status_code == 200
    assert "console.log" in res.text


# --- the /proxy bridge ------------------------------------------------------


def test_proxy_forwards_get_to_the_target_backend(console_dir: Path, upstream: str) -> None:
    client = _loopback_client(build_console_app(console_dir))
    res = client.get("/proxy/health", headers={"X-Vouch-Target": upstream})
    assert res.status_code == 200
    assert res.json()["path"] == "/health"
    assert _StubBackend.seen[-1] == {"method": "GET", "path": "/health", "auth": None}


def test_proxy_forwards_post_body_and_auth_header(console_dir: Path, upstream: str) -> None:
    client = _loopback_client(build_console_app(console_dir))
    res = client.post(
        "/proxy/rpc",
        headers={
            "X-Vouch-Target": upstream,
            "authorization": "Bearer sekret",
            "content-type": "application/json",
        },
        content=b'{"method":"kb.status"}',
    )
    assert res.status_code == 200
    assert _StubBackend.seen[-1]["path"] == "/rpc"
    assert _StubBackend.seen[-1]["body"] == '{"method":"kb.status"}'


def test_proxy_passes_through_a_backend_error_status(console_dir: Path, upstream: str) -> None:
    # a 401 from the backend must reach the browser as 401, not be masked as 502.
    client = _loopback_client(build_console_app(console_dir))
    res = client.get("/proxy/boom", headers={"X-Vouch-Target": upstream})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"


def test_proxy_requires_the_target_header(console_dir: Path) -> None:
    res = _loopback_client(build_console_app(console_dir)).get("/proxy/health")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_target"


def test_proxy_rejects_a_non_http_target(console_dir: Path) -> None:
    res = _loopback_client(build_console_app(console_dir)).get(
        "/proxy/health", headers={"X-Vouch-Target": "ftp://evil.example/x"}
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_target"


def test_proxy_rejects_non_loopback_client_by_default(console_dir: Path, upstream: str) -> None:
    app = build_console_app(console_dir)  # allow_remote defaults False
    remote = TestClient(app, client=("10.0.0.9", 1234))
    res = remote.get("/proxy/health", headers={"X-Vouch-Target": upstream})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"


def test_proxy_allows_remote_when_opted_in(console_dir: Path, upstream: str) -> None:
    app = build_console_app(console_dir, allow_remote=True)
    remote = TestClient(app, client=("10.0.0.9", 1234))
    res = remote.get("/proxy/health", headers={"X-Vouch-Target": upstream})
    assert res.status_code == 200


# --- console-directory resolution ------------------------------------------


def test_resolve_prefers_the_packaged_console(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "index.html").write_text("packaged", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "index.html").write_text("repo", encoding="utf-8")
    assert resolve_console_dir(packaged=pkg, repo_dist=repo) == pkg


def test_resolve_falls_back_to_repo_dist(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"  # never created
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "index.html").write_text("repo", encoding="utf-8")
    assert resolve_console_dir(packaged=pkg, repo_dist=repo) == repo


def test_resolve_is_none_when_no_console_is_built(tmp_path: Path) -> None:
    assert resolve_console_dir(packaged=tmp_path / "a", repo_dist=tmp_path / "b") is None


def test_serve_console_errors_clearly_when_no_console_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # no built SPA anywhere → a clean, actionable error, never a traceback deep
    # inside uvicorn (which must not even be reached).
    monkeypatch.setattr(console_mod, "resolve_console_dir", lambda **_k: None)
    with pytest.raises(console_mod.ConsoleError, match="npm run build"):
        console_mod.serve_console()


# --- the `vouch console` CLI command ---------------------------------------


def test_cli_console_rejects_non_loopback_bind_without_allow_remote() -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    res = CliRunner().invoke(cli, ["console", "--bind", "0.0.0.0:5173", "--no-open-browser"])
    assert res.exit_code != 0
    assert "allow-remote" in res.output.lower()


def test_cli_console_errors_cleanly_when_no_console_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    # loopback bind, but nothing is built → a clean click error, never a server.
    monkeypatch.setattr(console_mod, "resolve_console_dir", lambda **_k: None)
    res = CliRunner().invoke(cli, ["console", "--no-open-browser"])
    assert res.exit_code != 0
    assert "npm run build" in res.output
