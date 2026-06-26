"""FastAPI app for the review console (full #194 spec).

The web layer is a *viewport* over the existing kb.* surface — every
approve/reject/contradict/supersede goes through ``vouch.proposals`` /
``vouch.lifecycle`` so the audit log is identical regardless of whether the
action came from the CLI or the browser. There is no parallel data path and
no new on-disk schema.

What this module ships, mapped to the issue's acceptance criteria:

* **Queue, claim, audit** views (carried over from the MVP slice) plus the
  spec's **/session/<id>** (proposals grouped by agent run) and
  **/sources/<id>** (reverse index: which claims cite a source).
* **Server-side pagination** so a 500-item queue renders its first page fast
  — the template only ever materialises one page of rows.
* A **single WebSocket channel per KB** (``/ws``): every mutation broadcasts a
  tiny ``{"type": "refresh", ...}`` frame so a second reviewer's queue updates
  within a second without a manual refresh. The broadcast fires from the same
  request handler that performed the write, after it succeeds.
* **Bearer auth**: when the server is started with a token (``--auth``), every
  route except ``/healthz`` and ``/static`` requires it. Credentials are an
  ``Authorization: Bearer <token>`` header (CLI/API) or an HttpOnly cookie
  (browser). A ``?token=`` query param is a one-time GET bootstrap only — it's
  moved into the cookie and redirected away so the bare token never lingers in
  a URL or log. All comparisons are constant-time. Reviewer identity is the
  token's label. Loopback binds may run tokenless; a non-loopback bind without
  a token is refused at the CLI.
* **Progressive enhancement**: every action is a plain ``<form method=post>``,
  so the gate works with JavaScript disabled. The WebSocket + keyboard
  shortcuts are an additive layer on top.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import audit as audit_mod
from .. import lifecycle as life
from .. import proposals as proposals_mod
from ..models import Proposal, ProposalStatus
from ..storage import ArtifactNotFoundError, KBStore, _yaml_load, discover_root
from .dual_solve_api import register as _register_dual_solve

_MODULE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _MODULE_DIR / "templates"
_STATIC_DIR = _MODULE_DIR / "static"

_log = logging.getLogger("vouch.web")

# Default page size for the queue. A 500-item queue is 10 pages; the first
# page is the only thing rendered on the landing request.
DEFAULT_PAGE_SIZE = 50

# Per-client cap on a single WebSocket broadcast send. Keeps one slow/dead
# reviewer from stalling the approve/reject handler for everyone else.
_BROADCAST_TIMEOUT_S = 1.0


# --- realtime fan-out -----------------------------------------------------


@dataclass
class _Hub:
    """In-process WebSocket fan-out. One hub per app == one channel per KB.

    Reviewers connect to ``/ws``; whenever a mutation lands, the handling
    route calls :meth:`broadcast` and every connected client gets a small
    JSON frame telling it to re-pull the affected view. We deliberately send
    a *signal*, not the data — the client re-fetches through the same paginated
    routes, so there's exactly one rendering path and no risk of the socket
    payload drifting from the HTML.
    """

    _clients: set[WebSocket] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        text = json.dumps(message, separators=(",", ":"))
        async with self._lock:
            targets = list(self._clients)

        # Send to everyone concurrently, each with a short timeout. broadcast()
        # is awaited inline by the approve/reject handlers, so a single slow or
        # half-dead TCP socket must NOT be able to stall the decision for every
        # other reviewer — wait_for caps each send, and a timed-out or failed
        # client is dropped rather than blocking the gather.
        async def _send(ws: WebSocket) -> bool:
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=_BROADCAST_TIMEOUT_S)
                return True
            except Exception:
                return False

        results = await asyncio.gather(*(_send(ws) for ws in targets))
        dead = [ws for ws, ok in zip(targets, results, strict=True) if not ok]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)


# --- helpers --------------------------------------------------------------


def _is_review_event(name: str) -> bool:
    """The audit log carries every mutation; the timeline only shows
    review-gate decisions (approve / reject) and claim lifecycle moves."""
    if name.startswith("proposal.") and name.endswith((".approve", ".reject")):
        return True
    return name in {"claim.supersede", "claim.contradict",
                    "claim.archive", "claim.confirm"}


def _proposal_preview(payload: dict[str, Any]) -> str:
    """One-line preview shown in the queue. Mirrors the CLI's `pending` output."""
    for key in ("text", "title", "name"):
        value = payload.get(key)
        if value:
            return str(value).strip().splitlines()[0][:160]
    return "—"


def _paginate(total: int, page: int, page_size: int) -> tuple[int, int, int, int]:
    """Return ``(page, pages, start, end)`` clamped to valid bounds.

    ``page`` is 1-based. An out-of-range page clamps to the last page so a
    stale link (e.g. after items drained off the queue) never 404s.
    """
    page_size = max(1, page_size)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    end = min(start + page_size, total)
    return page, pages, start, end


def _pending_page(store: KBStore, page: int, page_size: int
                  ) -> tuple[list[Proposal], int, int, int]:
    """Load ONE page of pending proposals without parsing the whole queue.

    This is what keeps a 500-item queue's first page under the latency budget:
    ``store.list_proposals()`` deserialises every proposal file (~95% of the
    request time at 500 items), but the queue only ever shows one page. Pending
    proposals are exactly the files in ``proposed/`` (a decision moves the file
    to ``decided/``), so we glob the *filenames* — cheap — sort them, slice to
    the requested page, and only then parse that page's YAML.

    Returns ``(proposals, page, pages, total)`` with ``page`` clamped.

    A single corrupt/invalid proposal file must not 500 the whole queue: like
    ``health._load_claims_for_lint``, we skip a file that fails to load and
    log it, so one bad YAML can't take the review gate offline. Skipped files
    still count toward ``total`` so the page math (and "N proposals" header)
    stays honest about what's on disk.
    """
    proposed_dir = store.kb_dir / "proposed"
    paths = sorted(proposed_dir.glob("*.yaml")) if proposed_dir.is_dir() else []
    total = len(paths)
    page, pages, lo, hi = _paginate(total, page, page_size)
    proposals: list[Proposal] = []
    for p in paths[lo:hi]:
        try:
            proposals.append(Proposal.model_validate(_yaml_load(p.read_text())))
        except Exception as e:
            _log.warning("skipping unreadable proposal %s: %s", p.name, e)
    return proposals, page, pages, total


# --- auth -----------------------------------------------------------------


_TOKEN_COOKIE = "vouch_review_token"


@dataclass(frozen=True)
class AuthConfig:
    """Bearer-token gate. ``token is None`` means auth is disabled (loopback
    dev mode). ``label`` becomes the reviewer identity recorded in the audit
    log, so a team deployment attributes decisions to the token holder."""

    token: str | None = None
    label: str = "web-reviewer"

    @property
    def enabled(self) -> bool:
        return self.token is not None

    def matches(self, candidate: str | None) -> bool:
        """Constant-time token comparison.

        ``secrets.compare_digest`` runs in time dependent only on the shorter
        input's length, so an attacker can't recover the token byte-by-byte
        from response-timing differences. A plain ``==`` would leak it.
        """
        if self.token is None:
            return True
        return secrets.compare_digest(candidate or "", self.token)


def _bearer_header(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _cookie_token(request: Request) -> str | None:
    val = request.cookies.get(_TOKEN_COOKIE)
    return val.strip() if val else None


def _query_token(request: Request) -> str | None:
    # Only used for the one-time browser bootstrap; the handshake immediately
    # moves it into an HttpOnly cookie and redirects the query away (see
    # require_auth) so the bare token never lingers in a bookmarkable URL or
    # in access logs for any guarded route beyond that first hop.
    qp = request.query_params.get("token")
    return qp.strip() if qp else None


class _BootstrapRedirect(Exception):
    """Raised from ``require_auth`` when a valid token arrives via the query
    string. The registered handler converts it into a 303 that sets the
    HttpOnly cookie and strips ``?token=`` from the URL."""

    def __init__(self, *, location: str, token: str) -> None:
        self.location = location
        self.token = token


# --- app factory ----------------------------------------------------------


def build_app(
    kb_root: str | None = None,
    *,
    auth: AuthConfig | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    allow_dual_solve: bool = False,
    dual_solve_sandbox: bool = False,
    dual_solve_sandbox_image: str | None = None,
) -> FastAPI:
    """FastAPI app bound to a KB root.

    ``kb_root`` defaults to the nearest ``.vouch/`` discovered by walking up
    from ``cwd``. ``auth`` enables the Bearer gate; ``page_size`` controls
    queue pagination.
    """
    start = Path(kb_root).resolve() if kb_root else None
    # Resolve once at construction so every request hits the same store and a
    # bad root fails here with a clear error rather than a per-request 500.
    root = discover_root(start)
    store = KBStore(root)
    auth = auth or AuthConfig()
    hub = _Hub()

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app = FastAPI(title="vouch review-ui", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.state.store = store
    app.state.hub = hub
    app.state.auth = auth

    def reviewer() -> str:
        """Reviewer identity. With Bearer auth the token's label wins; without
        it we fall back to the same env var the CLI uses, so audit-log
        attribution stays consistent across surfaces."""
        if auth.enabled:
            return auth.label
        return os.environ.get("VOUCH_AGENT", "web-reviewer")

    def require_auth(request: Request) -> None:
        """Dependency: enforce the Bearer token when auth is enabled.

        Steady-state credentials are the ``Authorization: Bearer`` header
        (for API/CLI callers) or the ``HttpOnly`` cookie (for the browser).
        A token in the *query string* is accepted only as a one-time
        bootstrap: it's moved into the cookie and the query is stripped via a
        303 redirect (see :class:`_BootstrapRedirect`), so the bare token
        never lingers in a bookmarkable URL or in access logs on any guarded
        route past that first hop. All comparisons are constant-time.
        """
        if not auth.enabled:
            return
        if auth.matches(_bearer_header(request)) or auth.matches(_cookie_token(request)):
            return
        q = _query_token(request)
        if q is not None and auth.matches(q):
            # Only the safe navigation (GET/HEAD) gets the cookie-bootstrap
            # redirect — a 303 on a POST would drop the body. A valid query
            # token on any other method authenticates inline.
            if request.method in ("GET", "HEAD"):
                stripped = request.url.remove_query_params("token")
                location = stripped.path + (f"?{stripped.query}" if stripped.query else "")
                raise _BootstrapRedirect(location=location or "/", token=q)
            return
        raise HTTPException(
            status_code=401,
            detail="missing or invalid Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(_BootstrapRedirect)
    async def _bootstrap_redirect(request: Request, exc: _BootstrapRedirect) -> Any:
        # Move the bootstrap token into an HttpOnly, SameSite=Strict cookie and
        # redirect to the same path without the token query param. HttpOnly
        # keeps the token out of JS (so an XSS can't read it); SameSite=Strict
        # keeps it off cross-site requests. ``secure`` is left off so the
        # localhost-first (plain http) default still works; a TLS deployment
        # behind a proxy can set it via the proxy.
        resp = RedirectResponse(url=exc.location, status_code=303)
        resp.set_cookie(
            _TOKEN_COOKIE, exc.token,
            httponly=True, samesite="strict", path="/",
        )
        return resp

    guarded = [Depends(require_auth)]

    def _tmpl(request: Request, name: str, ctx: dict[str, Any]) -> Any:
        # Thread the auth-enabled flag into every template (display only — the
        # browser authenticates via the HttpOnly cookie, not via JS).
        ctx.setdefault("auth_enabled", auth.enabled)
        ctx.setdefault("dual_solve_enabled", allow_dual_solve)
        return templates.TemplateResponse(request, name, ctx)

    async def _notify(kind: str, **extra: Any) -> None:
        await hub.broadcast({"type": "refresh", "view": kind, **extra})

    # --- health ---

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "kb": str(store.root),
            "pending": len(store.list_proposals(ProposalStatus.PENDING)),
            "auth": auth.enabled,
            "clients": hub.client_count,
        }

    # --- queue (paginated) ---

    def _row(p: Proposal) -> dict[str, Any]:
        return {
            "id": p.id,
            "kind": p.kind.value,
            "proposed_by": p.proposed_by,
            "session_id": p.session_id,
            "proposed_at": p.proposed_at.isoformat(timespec="seconds"),
            "preview": _proposal_preview(p.payload),
        }

    @app.get("/", response_class=HTMLResponse, dependencies=guarded)
    def queue(request: Request, page: int = 1) -> Any:
        proposals, page, pages, total = _pending_page(store, page, page_size)
        return _tmpl(request, "queue.html", {
            "items": [_row(p) for p in proposals],
            "count": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "active": "queue",
        })

    # --- claim detail ---

    @app.get("/claim/{proposal_id}", response_class=HTMLResponse, dependencies=guarded)
    def claim_detail(request: Request, proposal_id: str) -> Any:
        try:
            pr = store.get_proposal(proposal_id)
        except ArtifactNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return _tmpl(request, "claim.html", {
            "proposal": pr.model_dump(mode="json"),
            "preview": _proposal_preview(pr.payload),
        })

    # --- session view: proposals grouped by agent run ---

    @app.get("/session/{session_id}", response_class=HTMLResponse, dependencies=guarded)
    def session_view(request: Request, session_id: str) -> Any:
        try:
            sess = store.get_session(session_id)
        except ArtifactNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        # Pull every proposal this session produced, regardless of status, so
        # a reviewer sees the whole run (pending + already-decided).
        proposals = []
        for pid in sess.proposal_ids:
            try:
                proposals.append(store.get_proposal(pid))
            except ArtifactNotFoundError:
                continue
        rows = [
            {
                "id": p.id,
                "kind": p.kind.value,
                "status": p.status.value,
                "preview": _proposal_preview(p.payload),
                "proposed_at": p.proposed_at.isoformat(timespec="seconds"),
            }
            for p in proposals
        ]
        return _tmpl(request, "session.html", {
            "session": {
                "id": sess.id,
                "agent": sess.agent,
                "task": sess.task,
                "started_at": sess.started_at.isoformat(timespec="seconds"),
                "ended_at": sess.ended_at.isoformat(timespec="seconds") if sess.ended_at else None,
                "note": sess.note,
            },
            "rows": rows,
            "pending_count": sum(1 for p in proposals if p.status == ProposalStatus.PENDING),
        })

    # --- source reverse-index view ---

    @app.get("/sources/{source_id}", response_class=HTMLResponse, dependencies=guarded)
    def source_view(request: Request, source_id: str) -> Any:
        try:
            src = store.get_source(source_id)
        except ArtifactNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        # Reverse index: every durable claim whose evidence cites this source.
        citing = [
            {"id": c.id, "text": c.text[:200], "status": c.status.value}
            for c in store.list_claims()
            if source_id in c.evidence
        ]
        return _tmpl(request, "source.html", {
            "source": {
                "id": src.id,
                "type": src.type.value,
                "locator": src.locator,
                "title": src.title,
                "media_type": src.media_type,
                "byte_size": src.byte_size,
                "created_at": src.created_at.isoformat(timespec="seconds"),
            },
            "citing": citing,
        })

    # --- mutations: approve / reject / contradict / supersede ---

    # These handlers are ``async`` so they can ``await`` the WebSocket
    # broadcast, but the underlying proposals/lifecycle calls are synchronous
    # filesystem + SQLite writes. Running them directly on the event loop would
    # block every other request (and the broadcast itself) for the duration of
    # the write; ``run_in_threadpool`` offloads them, which is what FastAPI
    # does for you automatically with sync route handlers.

    @app.post("/approve/{proposal_id}", dependencies=guarded)
    async def approve(proposal_id: str, reason: str | None = Form(default=None)) -> Any:
        try:
            artifact = await run_in_threadpool(
                proposals_mod.approve,
                store, proposal_id, approved_by=reviewer(), reason=reason,
            )
        except (proposals_mod.ProposalError, ArtifactNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await _notify("queue", action="approve", proposal_id=proposal_id,
                      artifact_id=getattr(artifact, "id", None))
        return RedirectResponse(url="/", status_code=303)

    @app.post("/reject/{proposal_id}", dependencies=guarded)
    async def reject(proposal_id: str, reason: str = Form(...)) -> Any:
        if not reason.strip():
            raise HTTPException(status_code=400, detail="reason is required")
        try:
            await run_in_threadpool(
                proposals_mod.reject,
                store, proposal_id, rejected_by=reviewer(), reason=reason,
            )
        except (proposals_mod.ProposalError, ArtifactNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await _notify("queue", action="reject", proposal_id=proposal_id)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/contradict/{claim_id}", dependencies=guarded)
    async def contradict(claim_id: str, against: str = Form(...)) -> Any:
        """Mark two durable claims as contradicting each other — a gate action
        the spec lists alongside approve/reject. Routes through lifecycle so
        the audit entry is identical to ``vouch contradict``."""
        try:
            await run_in_threadpool(
                life.contradict, store,
                claim_a=claim_id, claim_b=against, actor=reviewer(),
            )
        except (life.LifecycleError, ArtifactNotFoundError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await _notify("audit", action="contradict", claim_id=claim_id)
        return RedirectResponse(url="/audit", status_code=303)

    # --- audit timeline ---

    @app.get("/audit", response_class=HTMLResponse, dependencies=guarded)
    def audit(request: Request, limit: int = 100) -> Any:
        from ..scoping import viewer_from

        viewer = viewer_from(config_path=store.config_path)
        events = list(audit_mod.read_events(store.kb_dir, store=store, viewer=viewer))
        events.reverse()  # newest first
        filtered = [e for e in events if _is_review_event(e.event)][:limit]
        rows = [
            {
                "id": e.id,
                "event": e.event,
                "actor": e.actor,
                "object_ids": e.object_ids,
                "at": e.created_at.isoformat(timespec="seconds"),
                "reason": e.data.get("reason"),
            }
            for e in filtered
        ]
        return _tmpl(request, "audit.html", {"rows": rows, "count": len(rows),
                                             "active": "audit"})

    # --- JSON API (machine-readable + what the HTMX/JS layer polls) ---

    @app.get("/api/pending", dependencies=guarded)
    def api_pending(page: int = 1) -> JSONResponse:
        proposals, page, pages, total = _pending_page(store, page, page_size)
        return JSONResponse({
            "count": total,
            "page": page,
            "pages": pages,
            "items": [_row(p) for p in proposals],
        })

    # --- realtime channel ---

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # A browser can't set an Authorization header on a WebSocket, but it
        # *does* send the same-origin HttpOnly cookie on the handshake, so the
        # cookie is the primary credential. ?token= remains accepted for
        # non-browser clients (the CLI, tests). Constant-time compare.
        if auth.enabled:
            tok = websocket.cookies.get(_TOKEN_COOKIE) or websocket.query_params.get("token")
            if not auth.matches(tok):
                await websocket.close(code=4401)
                return
        await hub.connect(websocket)
        try:
            # Greet the client so it can confirm the channel is live, then idle
            # — the server only ever pushes; client messages are ignored.
            await websocket.send_text(json.dumps({"type": "hello",
                                                  "kb": str(store.root)}))
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            with contextlib.suppress(Exception):
                await hub.disconnect(websocket)

    _register_dual_solve(
        app, store=store, hub=hub, auth=auth, guarded=guarded,
        render=_tmpl, reviewer=reviewer, enabled=allow_dual_solve,
        sandboxed=dual_solve_sandbox, sandbox_image=dual_solve_sandbox_image,
    )
    return app
