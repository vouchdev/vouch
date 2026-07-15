"""Serve the vendored React review console (the `webapp/` SPA) from vouch.

The console is a static single-page app. It cannot call vouch cross-origin —
vouch deliberately sends no CORS headers — so it reaches every backend through
a same-origin ``/proxy/*`` bridge, passing the real endpoint in an
``X-Vouch-Target`` header. In dev that bridge is a vite plugin
(`webapp/plugins/vouch-proxy.ts`); this module reimplements it in Python so a
single ``pip install 'vouch-kb[web]'`` can serve the built SPA with no node.

This is a *viewport*: the bridge only forwards bytes to a `vouch serve
--transport http` backend, which remains the sole path to the review gate.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

_MODULE_DIR = Path(__file__).resolve().parent

# Loopback peers a same-origin browser client can present. The bridge is
# refused to anything else unless the operator explicitly opts in — a
# third-party page must not be able to drive a local reviewer's backends.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# Methods the SPA actually uses are GET (health/capabilities) and POST (rpc);
# accept the common verbs so the bridge stays transparent.
_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

# A generous ceiling: an rpc that runs the compile/summary LLM is synchronous.
_PROXY_TIMEOUT = 300.0


class ConsoleError(RuntimeError):
    """The console cannot be served (no built SPA, or a missing dependency)."""


def _default_repo_dist() -> Path:
    """`webapp/dist` relative to a source checkout (…/src/vouch/web → repo)."""
    return _MODULE_DIR.parents[2] / "webapp" / "dist"


def resolve_console_dir(
    *, packaged: Path | None = None, repo_dist: Path | None = None
) -> Path | None:
    """Locate the built console assets, or ``None`` if none are built.

    Prefers the copy bundled inside the wheel (``vouch/web/console``); falls
    back to ``webapp/dist`` in a source checkout. Mirrors how
    ``install_adapter`` prefers the repo tree over the packaged copy.
    """
    packaged = packaged if packaged is not None else _MODULE_DIR / "console"
    if (packaged / "index.html").is_file():
        return packaged
    repo_dist = repo_dist if repo_dist is not None else _default_repo_dist()
    if (repo_dist / "index.html").is_file():
        return repo_dist
    return None


def _err(status: int, code: str, message: str) -> JSONResponse:
    """The vouch-native error envelope the SPA already understands."""
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}}, status_code=status
    )


# Target hosts the bridge may forward to when no allowlist is configured. The
# bridge copies the caller's Authorization header onto the forwarded request,
# so an unrestricted target turns the console into a token-forwarding SSRF
# relay to any host the server can reach (including cloud metadata endpoints).
_LOOPBACK_TARGET_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _normalize_origin(raw: str) -> str:
    p = urllib.parse.urlparse(raw)
    return f"{p.scheme}://{p.netloc}".lower()


def _target_allowed(
    parsed: urllib.parse.ParseResult, allowed_origins: frozenset[str]
) -> bool:
    """Whether the bridge may forward to ``parsed``.

    With a configured allowlist the target's origin (scheme://host:port) must
    be one of the configured serve origins. With no allowlist the target must
    be loopback — the safe default for the local console, and never an
    arbitrary host the auth header could be leaked to.
    """
    if allowed_origins:
        return f"{parsed.scheme}://{parsed.netloc}".lower() in allowed_origins
    return (parsed.hostname or "").lower() in _LOOPBACK_TARGET_HOSTS


def build_console_app(
    console_dir: Path,
    *,
    allow_remote: bool = False,
    allowed_targets: tuple[str, ...] = (),
) -> Starlette:
    """Build the ASGI app: the ``/proxy/*`` bridge + the static SPA.

    ``allow_remote`` drops the loopback guard on the bridge — only for
    deliberately-exposed deployments behind their own auth. ``allowed_targets``
    is the set of serve origins (scheme://host:port) the bridge may forward to;
    empty means loopback-only.
    """
    root = console_dir.resolve()
    index = root / "index.html"
    allowed_origins = frozenset(_normalize_origin(t) for t in allowed_targets if t)

    async def _proxy(request: Request) -> Response:
        client_host = request.client.host if request.client else None
        if not allow_remote and client_host not in _LOOPBACK:
            return _err(403, "forbidden", "proxy is only available to loopback clients")

        target_raw = request.headers.get("x-vouch-target")
        if not target_raw:
            return _err(400, "bad_target", "missing X-Vouch-Target header")
        parsed = urllib.parse.urlparse(target_raw)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return _err(400, "bad_target", f"not a valid http(s) target: {target_raw}")
        if not _target_allowed(parsed, allowed_origins):
            return _err(
                403, "forbidden_target",
                f"target host not allowed: {target_raw} — the bridge forwards "
                "only to the configured serve origin(s), or loopback by default",
            )

        # The path after /proxy is appended to the target's host:port; any path
        # on the target itself is dropped, matching vouch-proxy.ts exactly.
        sub = request.url.path[len("/proxy") :] or "/"
        fwd_url = f"{parsed.scheme}://{parsed.netloc}{sub}"
        if request.url.query:
            fwd_url += f"?{request.url.query}"

        fwd_headers: dict[str, str] = {}
        if request.headers.get("content-type"):
            fwd_headers["content-type"] = request.headers["content-type"]
        if request.headers.get("authorization"):
            fwd_headers["authorization"] = request.headers["authorization"]
        body = await request.body()
        method = request.method

        def _do() -> tuple[int, str, bytes]:
            req = urllib.request.Request(
                fwd_url, data=body or None, method=method, headers=fwd_headers
            )
            try:
                with urllib.request.urlopen(req, timeout=_PROXY_TIMEOUT) as resp:
                    ctype = resp.headers.get("content-type", "application/json")
                    return resp.status, ctype, resp.read()
            except urllib.error.HTTPError as exc:
                # A backend 4xx/5xx is a real answer — pass it through unchanged
                # rather than masking it as a proxy 502.
                ctype = exc.headers.get("content-type", "application/json") if exc.headers else (
                    "application/json"
                )
                return exc.code, ctype, exc.read()

        try:
            status, ctype, payload = await run_in_threadpool(_do)
        except urllib.error.URLError as exc:
            return _err(502, "proxy_error", str(exc.reason))
        return Response(content=payload, status_code=status, media_type=ctype)

    async def _spa(request: Request) -> Response:
        """Serve a real asset, else index.html so client-side routing works."""
        rel = request.path_params.get("full_path", "")
        if rel:
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(root)  # reject path-traversal escapes
            except ValueError:
                candidate = index
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index)

    routes = [
        Route("/proxy", _proxy, methods=_PROXY_METHODS),
        Route("/proxy/{path:path}", _proxy, methods=_PROXY_METHODS),
        Route("/{full_path:path}", _spa, methods=["GET", "HEAD"]),
    ]
    return Starlette(routes=routes)


def serve_console(
    *,
    host: str = "127.0.0.1",
    port: int = 5173,
    allow_remote: bool = False,
    allowed_targets: tuple[str, ...] = (),
    console_dir: Path | None = None,
) -> None:
    """Serve the console with uvicorn (blocks). Raises ``ConsoleError`` early
    if no built SPA can be found, before uvicorn is ever started.

    ``allowed_targets`` restricts the ``/proxy`` bridge to those serve origins;
    empty means loopback-only.
    """
    resolved = console_dir if console_dir is not None else resolve_console_dir()
    if resolved is None:
        raise ConsoleError(
            "no built vouch console found. from a source checkout run "
            "`npm run build` in webapp/; otherwise install a release wheel of "
            "vouch-kb[web] (the console ships inside it)."
        )
    import uvicorn

    app = build_console_app(
        resolved, allow_remote=allow_remote, allowed_targets=allowed_targets
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
