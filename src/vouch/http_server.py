"""HTTP transport for `vouch serve` (VEP-0004).

A long-lived HTTP front-end over the same `kb.*` dispatch table the MCP and
JSONL transports use — so multiple clients can share one KB without each
spawning a local subprocess.

Endpoints:
  POST /rpc          JSONL envelope in, JSONL envelope out (see jsonl_server)
  GET  /capabilities kb.capabilities JSON (unauthenticated)
  GET  /healthz      {"ok": true} liveness (unauthenticated)

Safe by default: binds 127.0.0.1 and refuses any non-loopback bind unless
both --allow-public and a bearer token are given. The token gates /rpc only;
comparison is constant-time. There is no in-process TLS — terminate TLS at a
reverse proxy for public deployments. The review gate is unchanged: HTTP
clients file proposals exactly like every other transport.
"""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import jsonl_server
from .capabilities import capabilities as build_caps

DEFAULT_PORT = 8731
MAX_BODY_BYTES = 4 * 1024 * 1024  # reject oversized bodies before reading
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class _VouchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    token: str | None = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "vouch-http/0.1"
    # Bound the read window per request so a slow / drip-feed client can't
    # park a worker thread forever. Matters more once --allow-public is on.
    timeout = 30

    # --- helpers ----------------------------------------------------------

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, err_code: str, message: str) -> None:
        self._send_json(code, {"ok": False, "error": {"code": err_code, "message": message}})

    def _authorized(self) -> bool:
        token = self.server.token  # type: ignore[attr-defined]
        if token is None:
            return True
        provided = self.headers.get("Authorization", "")
        return hmac.compare_digest(provided, f"Bearer {token}")

    def log_message(self, *args: Any) -> None:
        # Silence the default per-request stderr access log.
        pass

    # --- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
        elif self.path == "/capabilities":
            self._send_json(200, build_caps().model_dump(mode="json"))
        else:
            self._error(404, "not_found", f"no such path: {self.path}")

    def do_POST(self) -> None:
        if self.path != "/rpc":
            self._error(404, "not_found", f"no such path: {self.path}")
            return
        if not self._authorized():
            self._error(401, "unauthorized", "missing or invalid bearer token")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._error(400, "invalid_request", "bad Content-Length")
            return
        if length < 0:
            # int() happily accepts "-1"; without this guard `rfile.read(-1)`
            # would read until EOF — unbounded body even with MAX_BODY_BYTES
            # set, since the size check below only catches positive overruns.
            self._error(400, "invalid_request", "Content-Length cannot be negative")
            return
        if length > MAX_BODY_BYTES:
            self._error(413, "invalid_request", "request body too large")
            return
        raw = self.rfile.read(length)
        try:
            envelope = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            self._error(400, "invalid_request", f"invalid JSON: {e}")
            return
        if not isinstance(envelope, dict):
            self._error(400, "invalid_request", "envelope must be a JSON object")
            return

        # Attribute the audit actor to the X-Vouch-Agent header for this
        # request only (thread-local via ContextVar — see jsonl_server._actor).
        agent = self.headers.get("X-Vouch-Agent")
        reset = jsonl_server._actor.set(agent) if agent else None
        try:
            response = jsonl_server.handle_request(envelope)
        finally:
            if reset is not None:
                jsonl_server._actor.reset(reset)
        self._send_json(200, response)


def make_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    token: str | None = None,
    allow_public: bool = False,
) -> _VouchHTTPServer:
    """Build (but don't start) the HTTP server, enforcing the bind policy."""
    if host not in _LOOPBACK_HOSTS and not (allow_public and token):
        raise RuntimeError(
            f"refusing to bind non-loopback host {host!r} without both "
            "--allow-public and a --token (set VOUCH_HTTP_TOKEN or pass --token)"
        )
    server = _VouchHTTPServer((host, port), _Handler)
    server.token = token
    return server


def run_http(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    token: str | None = None,
    allow_public: bool = False,
) -> None:
    """Serve the kb.* surface over HTTP until interrupted."""
    server = make_server(host, port, token=token, allow_public=allow_public)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
