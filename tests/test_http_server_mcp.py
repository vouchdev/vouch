"""MCP-spec Streamable HTTP transport (vouchdev/vouch#176, VEP-0004 final).

The HTTP transport shipped in #94/#104 spoke a custom JSON-RPC envelope at
`/rpc`. That works for vouch-aware clients but doesn't satisfy the five
Claude surfaces (Claude.ai Custom Connectors, Claude mobile write, Managed
Agents, Messages-API `mcp_servers`, Computer Use) — each requires a server
that implements MCP-over-Streamable-HTTP as published by Anthropic.

These tests pin the spec-compliant behaviour:

* `/mcp` (and `/messages` alias) speak JSON-RPC 2.0 with the MCP method set
  (`initialize`, `tools/list`, `tools/call`).
* The bearer-token gate covers the new endpoints with the same constant-time
  comparison the legacy `/rpc` uses, and supports multiple accepted tokens
  for fleets where each agent has its own credential.
* `/health` is added as an alias for `/healthz`, because the Claude.ai
  connector validator probes the former.
* The existing `/rpc`, `/healthz`, and `/capabilities` paths still behave
  exactly as before so tooling pinned to them keeps working.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from vouch.http_server import make_server
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    src = s.put_source(b"jwt notes")
    s.put_claim(Claim(id="c1", text="JWT rotation", evidence=[src.id]))
    return s


def _serve(server) -> Iterator[str]:  # type: ignore[no-untyped-def]
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


def _post(url: str, path: str, payload: dict, headers: dict | None = None,
          extra_accept: str | None = None) -> tuple[int, dict]:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if extra_accept:
        h["Accept"] = extra_accept
    if headers:
        h.update(headers)
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "event-stream" in ctype:
                # MCP-spec server may reply as SSE; pull out the `data:` JSON.
                payload_text = ""
                for line in body.decode().splitlines():
                    if line.startswith("data: "):
                        payload_text = line[len("data: "):]
                        break
                return resp.status, json.loads(payload_text or "{}")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(url: str, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url + path) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# --- /mcp: spec-compliant JSON-RPC 2.0 surface ----------------------------


def _initialize(url: str, headers: dict | None = None,
                path: str = "/mcp") -> tuple[int, dict, dict[str, str]]:
    """MCP `initialize` handshake — the entry point every client makes."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.0.1"},
        },
    }
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            if "event-stream" in resp.headers.get("Content-Type", ""):
                txt = next(
                    (ln[6:] for ln in body.decode().splitlines() if ln.startswith("data: ")),
                    "{}",
                )
                obj = json.loads(txt)
            else:
                obj = json.loads(body)
            return resp.status, obj, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def test_mcp_initialize_responds_with_spec_shape(base_url: str) -> None:
    code, body, _hdrs = _initialize(base_url)
    assert code == 200, body
    assert body.get("jsonrpc") == "2.0"
    assert body.get("id") == 1
    result = body.get("result") or {}
    # The MCP spec requires these three fields on a successful initialize.
    assert "protocolVersion" in result
    assert "capabilities" in result
    info = result.get("serverInfo") or {}
    assert info.get("name", "").lower() in {"vouch", "vouch-kb"}


def test_mcp_alias_messages_path_works(base_url: str) -> None:
    """Claude.ai's connector validator probes `/messages` historically;
    the new server exposes both paths as the same handler so old and new
    Claude surfaces work without per-tenant URL configuration."""
    code, body, _ = _initialize(base_url, path="/messages")
    assert code == 200, body
    assert body.get("jsonrpc") == "2.0"


def test_mcp_tools_list_returns_kb_surface(base_url: str) -> None:
    # 1. initialize first to satisfy the protocol.
    _initialize(base_url)
    # 2. tools/list — must include kb_status as a sanity check.
    code, body = _post(base_url, "/mcp", {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
    })
    assert code == 200, body
    result = body.get("result") or {}
    tools = result.get("tools") or []
    names = {t.get("name") for t in tools}
    # If the kb.* tool surface isn't exposed via tools/list, the entire
    # point of this PR is broken.
    assert "kb_status" in names, f"kb_status missing — got {sorted(names)[:6]}…"
    assert len(tools) >= 20, f"expected ≥20 kb.* tools, got {len(tools)}"


def test_mcp_accepts_session_id_header_in_stateless_mode(base_url: str) -> None:
    """A stateful client (e.g. one that pinned `Mcp-Session-Id` from a prior
    deployment) must not be rejected when we switch to stateless mode.

    The server runs ``stateless=True`` so each request is independent; clients
    that *do* send the header should see it silently ignored rather than 4xx-d.
    Regression for the PR #177 review question.
    """
    _initialize(base_url)
    code, body = _post(
        base_url, "/mcp",
        {"jsonrpc": "2.0", "id": 99, "method": "tools/list"},
        headers={"Mcp-Session-Id": "stale-client-session-from-yesterday"},
    )
    assert code == 200, body
    assert body.get("jsonrpc") == "2.0"
    assert body.get("id") == 99


def test_mcp_tools_call_kb_status_round_trip(base_url: str, kb: KBStore) -> None:
    _initialize(base_url)
    code, body = _post(base_url, "/mcp", {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "kb_status", "arguments": {}},
    })
    assert code == 200, body
    # MCP responses for tools/call carry a `content` array; FastMCP also
    # populates `structuredContent` with the raw return value. Either is
    # acceptable as long as `claims` round-trips.
    result = body.get("result") or {}
    sc = result.get("structuredContent") or {}
    if "claims" in sc:
        assert sc["claims"] == 1
        return
    # Fall back to the textual `content` channel.
    content_blocks = result.get("content") or []
    text = next((b.get("text", "") for b in content_blocks if b.get("type") == "text"), "")
    assert '"claims"' in text and "1" in text, text


# --- bearer auth, including multi-token + env-var refs --------------------


def test_mcp_without_bearer_when_token_required(kb: KBStore) -> None:
    gen = _serve(make_server("127.0.0.1", 0, token="s3cret"))
    url = next(gen)
    try:
        code, _body, _ = _initialize(url)
        assert code == 401
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def test_mcp_accepts_any_of_multiple_bearer_tokens(kb: KBStore) -> None:
    """Fleet operators want one server with one accept-list — every agent in
    the fleet gets its own token, but the server validates by membership."""
    gen = _serve(make_server("127.0.0.1", 0, tokens=["alpha", "beta", "gamma"]))
    url = next(gen)
    try:
        # alpha works
        ok, _, _ = _initialize(url, headers={"Authorization": "Bearer alpha"})
        assert ok == 200
        # beta works
        ok, _, _ = _initialize(url, headers={"Authorization": "Bearer beta"})
        assert ok == 200
        # delta does NOT work (not in the accept-list)
        bad, _, _ = _initialize(url, headers={"Authorization": "Bearer delta"})
        assert bad == 401
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def test_health_alias_unauthenticated_even_with_token(kb: KBStore) -> None:
    gen = _serve(make_server("127.0.0.1", 0, token="s3cret"))
    url = next(gen)
    try:
        assert _get(url, "/healthz")[0] == 200
        # /health is the alias used by Claude.ai's connector validator.
        assert _get(url, "/health")[0] == 200
        assert _get(url, "/capabilities")[0] == 200
    finally:
        with pytest.raises(StopIteration):
            next(gen)


# --- config.yaml `serve:` section parsing ---------------------------------


def test_serve_config_loads_bearer_tokens_list(tmp_path: Path) -> None:
    """The new YAML surface promised by #176:

        serve:
          bearer_tokens:
            - alpha
            - beta
    """
    from vouch.http_server import load_serve_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "serve:\n"
        "  bearer_tokens:\n"
        "    - alpha\n"
        "    - beta\n",
    )
    sc = load_serve_config(cfg)
    assert sc.tokens == ["alpha", "beta"]


def test_serve_config_resolves_env_ref_for_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bearer_token: env:VAR` — the env-ref form keeps the literal token
    out of any committed config file."""
    from vouch.http_server import load_serve_config

    monkeypatch.setenv("VOUCH_TOKEN", "secret-from-env")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("serve:\n  bearer_token: env:VOUCH_TOKEN\n")
    sc = load_serve_config(cfg)
    assert sc.tokens == ["secret-from-env"]


def test_serve_config_missing_env_ref_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vouch.http_server import ServeConfigError, load_serve_config

    monkeypatch.delenv("VOUCH_TOKEN_MISSING", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("serve:\n  bearer_token: env:VOUCH_TOKEN_MISSING\n")
    with pytest.raises(ServeConfigError, match="VOUCH_TOKEN_MISSING"):
        load_serve_config(cfg)


def test_serve_config_empty_returns_no_tokens(tmp_path: Path) -> None:
    """No `serve:` section at all is the most common case — must be benign."""
    from vouch.http_server import load_serve_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text("# nothing relevant here\n")
    sc = load_serve_config(cfg)
    assert sc.tokens == []


# --- regression: legacy /rpc, /healthz, /capabilities unchanged -----------


def test_legacy_rpc_envelope_still_works(base_url: str) -> None:
    """PR #104's custom `/rpc` envelope is what every existing vouch-aware
    client speaks — must not break."""
    code, body = _post(base_url, "/rpc", {"id": "r1", "method": "kb.status"})
    assert code == 200
    assert body["ok"] is True
    assert body["result"]["claims"] == 1


def test_legacy_healthz_unchanged(base_url: str) -> None:
    code, body = _get(base_url, "/healthz")
    assert code == 200 and body == {"ok": True}


def test_legacy_capabilities_unchanged(base_url: str) -> None:
    code, body = _get(base_url, "/capabilities")
    assert code == 200
    assert "http" in body["transports"]
