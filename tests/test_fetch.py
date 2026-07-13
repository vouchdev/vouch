"""URL snapshot intake (`vouch source fetch` / fetch.py)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.cli import cli
from vouch.fetch import FetchError, _addr_is_public, fetch_url, snapshot_url
from vouch.storage import KBStore


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/page":
            body = b"# hello from the fixture\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(b"x" * 4096)
        elif self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass


@pytest.fixture
def http_url() -> str:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_public_address_guard() -> None:
    assert _addr_is_public("93.184.216.34") is True
    for private in ("127.0.0.1", "10.0.0.8", "192.168.1.1", "169.254.1.1", "::1", "0.0.0.0"):
        assert _addr_is_public(private) is False, private


def test_fetch_refuses_non_http_and_private_hosts(http_url: str) -> None:
    with pytest.raises(FetchError):
        fetch_url("file:///etc/hostname")
    # loopback fixture is refused unless the test bypass is set
    with pytest.raises(FetchError):
        fetch_url(f"{http_url}/page")


def test_fetch_follows_redirects_and_caps_size(http_url: str) -> None:
    result = fetch_url(f"{http_url}/redirect", allow_private=True)
    assert result.content == b"# hello from the fixture\n"
    assert result.media_type == "text/markdown"
    assert result.final_url.endswith("/page")

    with pytest.raises(FetchError, match="snapshot cap"):
        fetch_url(f"{http_url}/big", allow_private=True, max_bytes=1024)

    with pytest.raises(FetchError, match="redirect"):
        fetch_url(f"{http_url}/loop", allow_private=True)


def test_snapshot_registers_content_addressed_source(
    tmp_path: Path, http_url: str,
) -> None:
    store = KBStore.init(tmp_path)
    src = snapshot_url(store, f"{http_url}/page", allow_private=True)

    assert (store.kb_dir / "sources" / src.id / "content").read_bytes() == (
        b"# hello from the fixture\n"
    )
    assert src.metadata["fetched_at"]
    assert src.metadata["final_url"].endswith("/page")

    # idempotent: same bytes -> same id
    again = snapshot_url(store, f"{http_url}/page", allow_private=True)
    assert again.id == src.id


def test_cli_source_fetch_records_audit_event(
    tmp_path: Path, http_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    KBStore.init(tmp_path)
    monkeypatch.chdir(tmp_path)
    # the CLI has no private-host bypass; point the guard off for the fixture
    monkeypatch.setattr("vouch.fetch._check_url", lambda url, allow_private: None)

    result = CliRunner().invoke(cli, ["source", "fetch", f"{http_url}/page"])
    assert result.exit_code == 0, result.output
    sid = result.output.strip().splitlines()[-1]

    store = KBStore(tmp_path)
    assert (store.kb_dir / "sources" / sid / "content").exists()
    audit_text = (store.kb_dir / "audit.log.jsonl").read_text(encoding="utf-8")
    assert "source.fetch" in audit_text
