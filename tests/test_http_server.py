"""HTTP transport (VEP-0004 / #94): dispatch, auth, bind policy, attribution."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from vouch.http_server import _VouchHTTPServer, make_server, run_http
from vouch.models import Claim, ProposalStatus
from vouch.storage import KBStore


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    # Handlers resolve the KB from cwd via discover_root().
    monkeypatch.chdir(s.root)
    src = s.put_source(b"jwt notes")
    s.put_claim(Claim(id="c1", text="JWT rotation", evidence=[src.id]))
    return s


def _serve(server: _VouchHTTPServer) -> Iterator[str]:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def base_url(kb: KBStore) -> Iterator[str]:
    yield from _serve(make_server("127.0.0.1", 0))


def _post(url: str, envelope: dict, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        url + "/rpc",
        data=json.dumps(envelope).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url: str, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url + path) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- dispatch -------------------------------------------------------------


def test_rpc_dispatches_to_kb_surface(base_url: str) -> None:
    code, body = _post(base_url, {"id": "r1", "method": "kb.status"})
    assert code == 200
    assert body["ok"] is True
    assert body["result"]["claims"] == 1


def test_rpc_unknown_method(base_url: str) -> None:
    _code, body = _post(base_url, {"id": "r2", "method": "kb.nope"})
    assert body["ok"] is False
    assert body["error"]["code"] == "method_not_found"


def test_rpc_invalid_json(base_url: str) -> None:
    req = urllib.request.Request(
        base_url + "/rpc", data=b"{not json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req)
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"]["code"] == "invalid_request"


# --- read-only GET routes -------------------------------------------------


def test_healthz(base_url: str) -> None:
    code, body = _get(base_url, "/healthz")
    assert code == 200 and body == {"ok": True}


def test_capabilities_advertises_http(base_url: str) -> None:
    code, body = _get(base_url, "/capabilities")
    assert code == 200
    assert "http" in body["transports"]
    assert "kb.search" in body["methods"]


# --- auth -----------------------------------------------------------------


def test_token_required_when_set(kb: KBStore) -> None:
    gen = _serve(make_server("127.0.0.1", 0, token="s3cret"))
    url = next(gen)
    try:
        code, body = _post(url, {"id": "r", "method": "kb.status"})
        assert code == 401 and body["error"]["code"] == "unauthorized"

        code, body = _post(
            url, {"id": "r", "method": "kb.status"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert code == 200 and body["ok"] is True

        code, body = _post(
            url, {"id": "r", "method": "kb.status"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert code == 401
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def test_get_routes_unauthenticated_even_with_token(kb: KBStore) -> None:
    gen = _serve(make_server("127.0.0.1", 0, token="s3cret"))
    url = next(gen)
    try:
        assert _get(url, "/healthz")[0] == 200
        assert _get(url, "/capabilities")[0] == 200
    finally:
        with pytest.raises(StopIteration):
            next(gen)


# --- bind policy ----------------------------------------------------------


def test_non_loopback_requires_allow_public_and_token() -> None:
    with pytest.raises(RuntimeError, match="non-loopback"):
        make_server("0.0.0.0", 0)
    with pytest.raises(RuntimeError, match="non-loopback"):
        make_server("0.0.0.0", 0, allow_public=True)  # token still missing


def test_non_loopback_allowed_with_token_and_flag() -> None:
    server = make_server("0.0.0.0", 0, token="t", allow_public=True)
    server.server_close()  # constructed without raising = policy satisfied


# --- audit attribution via X-Vouch-Agent ----------------------------------


def test_x_vouch_agent_sets_actor(kb: KBStore) -> None:
    src = kb.list_sources()[0]
    gen = _serve(make_server("127.0.0.1", 0))
    url = next(gen)
    try:
        code, body = _post(
            url,
            {"id": "r", "method": "kb.propose_claim",
             "params": {"text": "claim via http", "evidence": [src.id]}},
            headers={"X-Vouch-Agent": "http-bot"},
        )
        assert code == 200 and body["ok"] is True, body
    finally:
        with pytest.raises(StopIteration):
            next(gen)
    pending = kb.list_proposals(ProposalStatus.PENDING)
    assert any(p.proposed_by == "http-bot" for p in pending)


def test_run_http_rejects_public_bind_fast() -> None:
    # run_http surfaces the same guard before binding anything.
    with pytest.raises(RuntimeError, match="non-loopback"):
        run_http("0.0.0.0", 0)


# --- malformed Content-Length protection ----------------------------------


def test_negative_content_length_rejected(base_url: str) -> None:
    """Raw request — urllib won't reliably forward a negative Content-Length.

    Without the explicit guard, int("-1") succeeds and rfile.read(-1) reads
    until EOF, defeating MAX_BODY_BYTES. The server must respond with a 400
    invalid_request instead.
    """
    import socket

    host, _, port_s = base_url.removeprefix("http://").partition(":")
    port = int(port_s)
    raw_request = (
        b"POST /rpc HTTP/1.1\r\n"
        b"Host: " + host.encode() + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: -1\r\n"
        b"Connection: close\r\n\r\n"
    )
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(raw_request)
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk

    head, _, body = response.partition(b"\r\n\r\n")
    assert b" 400 " in head.split(b"\r\n", 1)[0], head
    payload = json.loads(body)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_request"
    assert "negative" in payload["error"]["message"].lower()
