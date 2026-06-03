"""HTTP/SSE transport for the vouch KB server (VEP-0004).

Wire format:

  POST /kb/<method-name>
  Content-Type: application/json
  Authorization: Bearer <token>
  { ...params... }
  → 200 { ...result... }  |  4xx/5xx { "error": { "code": "...", "message": "..." } }

  GET /kb/events?topics=proposals,approvals,lifecycle
  Authorization: Bearer <token>
  → text/event-stream

Dispatch reuses the same HANDLERS dict as jsonl_server.py — no duplication.

Auth modes: none | bearer | token-file  (see auth.py).
Actor is resolved from the token's configured name in config.yaml, falling
back to the VOUCH_AGENT env var.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from .auth import AuthError, assert_loopback_for_no_auth, require_auth, resolve_actor
from .jsonl_server import HANDLERS
from .proposals import ProposalError
from .storage import ArtifactNotFoundError, KBNotFoundError, KBStore, discover_root

try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response, StreamingResponse
    from starlette.routing import Route
    _STARLETTE_AVAILABLE = True
except ImportError:
    _STARLETTE_AVAILABLE = False


_ERROR_CODE_TO_STATUS: dict[str, int] = {
    "not_found": 404,
    "validation_error": 422,
    "auth_error": 401,
    "forbidden": 403,
    "method_not_found": 404,
    "missing_param": 422,
    "invalid_request": 422,
    "internal_error": 500,
}


def _error_response(code: str, message: str) -> JSONResponse:
    status = _ERROR_CODE_TO_STATUS.get(code, 500)
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _get_store() -> KBStore:
    try:
        return KBStore(discover_root())
    except KBNotFoundError as e:
        raise RuntimeError(str(e)) from e


def _fallback_actor() -> str:
    return os.environ.get("VOUCH_AGENT", "unknown-agent")


def _make_dispatch_handler(auth_mode: str) -> Any:
    async def dispatch(request: Request) -> Response:
        method_name = request.path_params["method"]
        full_method = f"kb.{method_name}"

        kb_dir: Path
        try:
            store = _get_store()
            kb_dir = store.kb_dir
        except RuntimeError as e:
            return _error_response("internal_error", str(e))

        auth_header = request.headers.get("authorization")
        try:
            token = require_auth(auth_header, auth_mode, kb_dir)
        except AuthError as e:
            return _error_response("auth_error", str(e))

        actor = resolve_actor(token, kb_dir, _fallback_actor()) if token else _fallback_actor()

        if full_method not in HANDLERS:
            return _error_response("method_not_found", f"unknown method: {full_method!r}")

        try:
            body = await request.body()
            params: dict[str, Any] = json.loads(body) if body else {}
        except json.JSONDecodeError as e:
            return _error_response("validation_error", f"invalid JSON body: {e}")

        saved_agent = os.environ.get("VOUCH_AGENT")
        os.environ["VOUCH_AGENT"] = actor
        try:
            result = HANDLERS[full_method](params)
        except KeyError as e:
            return _error_response("missing_param", str(e))
        except (ValueError, ProposalError, ArtifactNotFoundError) as e:
            return _error_response("invalid_request", str(e))
        except Exception:
            logger.exception("handler %s failed", full_method)
            return _error_response("internal_error", "internal server error")
        finally:
            if saved_agent is None:
                os.environ.pop("VOUCH_AGENT", None)
            else:
                os.environ["VOUCH_AGENT"] = saved_agent

        return JSONResponse(result)

    return dispatch


_TOPIC_PREFIXES: dict[str, tuple[str, ...]] = {
    "proposals": ("proposal.",),
    "approvals": ("proposal.approve",),
    "lifecycle": ("claim.", "page.", "entity.", "relation."),
    "all": (),
}


def _event_matches(event_name: str, topics: list[str]) -> bool:
    if not topics or "all" in topics:
        return True
    for topic in topics:
        prefixes = _TOPIC_PREFIXES.get(topic, (topic,))
        for prefix in prefixes:
            if event_name.startswith(prefix):
                return True
    return False


def _make_sse_handler(auth_mode: str) -> Any:
    async def sse_events(request: Request) -> Response:
        try:
            store = _get_store()
            kb_dir = store.kb_dir
        except RuntimeError as e:
            return _error_response("internal_error", str(e))

        auth_header = request.headers.get("authorization")
        try:
            require_auth(auth_header, auth_mode, kb_dir)
        except AuthError as e:
            return _error_response("auth_error", str(e))

        topics_param = request.query_params.get("topics", "all")
        topics = [t.strip() for t in topics_param.split(",") if t.strip()]

        audit_log = kb_dir / "audit.log.jsonl"

        async def generate() -> Any:
            yield b"retry: 1000\n\n"
            pos = audit_log.stat().st_size if audit_log.exists() else 0
            while True:
                await asyncio.sleep(0.5)
                if not audit_log.exists():
                    continue
                with audit_log.open("rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos_delta = fh.tell() - pos
                if pos_delta == 0:
                    continue
                pos += pos_delta
                for raw_line in chunk.splitlines():
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        record = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    event_name = record.get("event", "")
                    if not _event_matches(event_name, topics):
                        continue
                    data = json.dumps(record, default=str)
                    yield f"event: {event_name}\ndata: {data}\n\n".encode()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return sse_events


def build_app(auth_mode: str = "token-file") -> Starlette:
    """Build and return the Starlette ASGI app."""
    if not _STARLETTE_AVAILABLE:
        raise ImportError(
            "HTTP transport requires the 'http' optional dependencies: "
            "pip install 'vouch-kb[http]'"
        )

    routes = [
        Route("/kb/events", endpoint=_make_sse_handler(auth_mode), methods=["GET"]),
        Route("/kb/{method:path}", endpoint=_make_dispatch_handler(auth_mode), methods=["POST"]),
    ]
    return Starlette(routes=routes)


def run_http(bind: str = "127.0.0.1:7749", auth_mode: str = "token-file") -> None:
    """Start the HTTP server.  Called by `vouch serve --transport http`."""
    if not _STARLETTE_AVAILABLE:
        raise ImportError(
            "HTTP transport requires the 'http' optional dependencies: "
            "pip install 'vouch-kb[http]'"
        )

    try:
        import uvicorn
    except ImportError as e:
        raise ImportError(
            "HTTP transport requires uvicorn: pip install 'vouch-kb[http]'"
        ) from e

    if auth_mode == "none":
        assert_loopback_for_no_auth(bind)

    host, _, port_str = bind.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port_str) if port_str else 7749

    app = build_app(auth_mode=auth_mode)
    uvicorn.run(app, host=host, port=port)
