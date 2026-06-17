"""Confidence-gated push context — ``kb.volunteer_context`` (#236).

When a session opens with a task, vouch watches retrieval salience for
approved claims. Claims whose normalized relevance exceeds a configurable
threshold are queued and optionally pushed as MCP notifications.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from . import hot_memory
from .context import _RETRACTED_CLAIM_STATUSES
from .models import Session
from .scoping import ViewerContext, viewer_from
from .storage import ArtifactNotFoundError, KBStore

if TYPE_CHECKING:
    from mcp.server.session import ServerSession

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.85
DEFAULT_THROTTLE_SECONDS = 30.0
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_MAX_PER_SESSION = 50

_mcp_push: dict[str, tuple[ServerSession, asyncio.AbstractEventLoop]] = {}
_pending: dict[str, list[VolunteerOffer]] = {}
_watch_threads: dict[str, threading.Thread] = {}
_state_lock = threading.Lock()


@dataclass(frozen=True)
class VolunteerConfig:
    enabled: bool = True
    threshold: float = DEFAULT_THRESHOLD
    throttle_seconds: float = DEFAULT_THROTTLE_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL
    max_per_session: int = DEFAULT_MAX_PER_SESSION


@dataclass(frozen=True)
class VolunteerOffer:
    claim_id: str
    relevance: float
    why: str
    session_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "relevance": self.relevance,
            "why": self.why,
            "session_id": self.session_id,
        }


def load_config(store: KBStore) -> VolunteerConfig:
    """Read ``volunteer:`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text())
    except (OSError, yaml.YAMLError):
        return VolunteerConfig()
    if not isinstance(loaded, dict):
        return VolunteerConfig()
    raw = loaded.get("volunteer")
    if not isinstance(raw, dict):
        return VolunteerConfig()
    enabled = bool(raw.get("enabled", True))
    threshold = float(raw.get("threshold", DEFAULT_THRESHOLD))
    throttle = float(raw.get("throttle_seconds", DEFAULT_THROTTLE_SECONDS))
    poll = float(raw.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL))
    max_per = int(raw.get("max_per_session", DEFAULT_MAX_PER_SESSION))
    return VolunteerConfig(
        enabled=enabled,
        threshold=threshold,
        throttle_seconds=throttle,
        poll_interval_seconds=poll,
        max_per_session=max_per,
    )


def session_query(sess: Session) -> str | None:
    parts: list[str] = []
    if sess.task:
        parts.append(sess.task.strip())
    if sess.note:
        parts.append(sess.note.strip())
    joined = " ".join(parts).strip()
    return joined or None


def normalize_relevance(raw: float, backend: str, *, batch_max: float) -> float:
    if backend in ("embedding", "hybrid"):
        return max(0.0, min(1.0, raw))
    if batch_max <= 0.0:
        return 0.0
    return max(0.0, min(1.0, raw / batch_max))


def _retrieve_claim_scores(
    store: KBStore,
    query: str,
    viewer: ViewerContext,
    *,
    limit: int = 10,
) -> list[tuple[str, float, str, str]]:
    """Return (claim_id, raw_score, snippet, backend) for visible claims."""
    from .context import _retrieve

    hits = _retrieve(store, query, limit, viewer)
    claim_hits = [(k, i, s, sc, be) for k, i, s, sc, be in hits if k == "claim"]
    if not claim_hits:
        return []

    batch_max = max(sc for _, _, _, sc, _ in claim_hits)
    out: list[tuple[str, float, str, str]] = []
    for _, claim_id, snippet, score, backend in claim_hits:
        try:
            claim = store.get_claim(claim_id)
        except ArtifactNotFoundError:
            continue
        if claim.status in _RETRACTED_CLAIM_STATUSES:
            continue
        rel = normalize_relevance(score, backend, batch_max=batch_max)
        out.append((claim_id, rel, snippet, backend))
    out.sort(key=lambda row: row[1], reverse=True)
    return out


def _build_why(*, claim_id: str, query: str, relevance: float, backend: str, snippet: str) -> str:
    preview = snippet.replace("«", "").replace("»", "").strip()
    if preview:
        return (
            f"task mentions {query!r}; approved claim {claim_id!r} "
            f"matches ({backend} relevance {relevance:.2f}): {preview[:120]}"
        )
    return (
        f"task mentions {query!r}; approved claim {claim_id!r} "
        f"matches with {backend} relevance {relevance:.2f}"
    )


def evaluate_session(
    store: KBStore,
    sess: Session,
    *,
    config: VolunteerConfig | None = None,
    project: str | None = None,
) -> VolunteerOffer | None:
    """Return the best new volunteer offer for *sess*, or ``None``."""
    cfg = config or load_config(store)
    if not cfg.enabled:
        return None
    query = session_query(sess)
    if not query:
        return None

    mem = hot_memory.get(sess.id)
    if mem is None:
        return None
    if mem.push_count >= cfg.max_per_session:
        return None

    viewer = viewer_from(
        config_path=store.config_path,
        project=project or mem.project,
        agent=mem.agent,
    )
    scored = _retrieve_claim_scores(store, query, viewer)
    if not scored:
        return None

    scores = {cid: rel for cid, rel, _, _ in scored}
    hot_memory.update_snapshot(sess.id, scores)

    now = time.monotonic()
    if mem.last_push_at is not None and (now - mem.last_push_at) < cfg.throttle_seconds:
        return None

    for claim_id, relevance, snippet, backend in scored:
        if claim_id in mem.volunteered:
            continue
        if relevance < cfg.threshold:
            continue
        why = _build_why(
            claim_id=claim_id,
            query=query,
            relevance=relevance,
            backend=backend,
            snippet=snippet,
        )
        return VolunteerOffer(
            claim_id=claim_id,
            relevance=relevance,
            why=why,
            session_id=sess.id,
        )
    return None


def enqueue_offer(offer: VolunteerOffer) -> None:
    """Queue an offer and optionally push over MCP (used by tests)."""
    _enqueue_offer(offer)


def _enqueue_offer(offer: VolunteerOffer) -> None:
    with _state_lock:
        _pending.setdefault(offer.session_id, []).append(offer)
    hot_memory.mark_volunteered(offer.session_id, offer.claim_id, pushed_at=time.monotonic())
    _maybe_mcp_push(offer)


def _maybe_mcp_push(offer: VolunteerOffer) -> None:
    with _state_lock:
        push = _mcp_push.get(offer.session_id)
    if push is None:
        return
    session, loop = push

    async def _send() -> None:
        from typing import cast

        from mcp.types import Notification, ServerNotification

        try:
            await session.send_notification(
                cast(
                    ServerNotification,
                    Notification(
                        method="kb.volunteer_context",
                        params=offer.to_dict(),
                    ),
                )
            )
        except Exception:
            logger.exception("MCP volunteer_context push failed for %s", offer.session_id)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop)
    except RuntimeError:
        logger.exception("no event loop for MCP volunteer push")


def register_mcp_push(
    session_id: str,
    session: ServerSession,
    loop: asyncio.AbstractEventLoop,
) -> None:
    with _state_lock:
        _mcp_push[session_id] = (session, loop)


def drain_pending(session_id: str, *, clear: bool = True) -> list[VolunteerOffer]:
    """Return queued offers for *session_id* (poll surface for JSONL / CLI)."""
    with _state_lock:
        if clear:
            return _pending.pop(session_id, [])
        return list(_pending.get(session_id, []))


def on_session_start(store: KBStore, sess: Session) -> None:
    """Register hot memory and start the background watch when *sess* has a task."""
    cfg = load_config(store)
    if not cfg.enabled:
        return
    query = session_query(sess)
    if not query:
        return

    hot_memory.register(
        session_id=sess.id,
        query=query,
        agent=sess.agent,
    )
    try:
        offer = evaluate_session(store, sess, config=cfg)
        if offer is not None:
            _enqueue_offer(offer)
    except Exception:
        logger.exception("initial volunteer evaluation failed for %s", sess.id)
    _start_watch(store, sess.id, cfg)


def on_session_end(session_id: str) -> None:
    hot_memory.unregister(session_id)
    with _state_lock:
        _mcp_push.pop(session_id, None)
        _pending.pop(session_id, None)
    thread = _watch_threads.pop(session_id, None)
    if thread is not None and thread.is_alive():
        # ``unregister`` sets ``active=False``; the loop exits on next check.
        thread.join(timeout=0.1)


def _start_watch(store: KBStore, session_id: str, cfg: VolunteerConfig) -> None:
    existing = _watch_threads.get(session_id)
    if existing is not None and existing.is_alive():
        return

    def _loop() -> None:
        while True:
            mem = hot_memory.get(session_id)
            if mem is None or not mem.active:
                break
            try:
                sess = store.get_session(session_id)
                offer = evaluate_session(store, sess, config=cfg)
                if offer is not None:
                    _enqueue_offer(offer)
            except Exception:
                logger.exception("volunteer watch failed for session %s", session_id)
            mem = hot_memory.get(session_id)
            if mem is None or not mem.active:
                break
            time.sleep(cfg.poll_interval_seconds)

    thread = threading.Thread(
        target=_loop,
        name=f"vouch-volunteer-{session_id}",
        daemon=True,
    )
    _watch_threads[session_id] = thread
    thread.start()


def evaluate_now(store: KBStore, session_id: str) -> VolunteerOffer | None:
    """Synchronous single-shot evaluation (tests and immediate poll)."""
    sess = store.get_session(session_id)
    offer = evaluate_session(store, sess)
    if offer is not None:
        _enqueue_offer(offer)
    return offer
