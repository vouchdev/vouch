"""Entity-salience retrieval reflex — auto-prefetch claim candidates.

Keeps a per-session, in-memory ring buffer of the recent query strings a
caller passed to the read path. On each read it runs a zero-LLM entity pass
(substring match on entity name/aliases, plus FTS via the existing index
when available) over the buffered queries and attaches the top-K matched
entities — with the claims that reference them — as ``_meta.vouch_salience``.

Design constraints (issue #223):
  - zero LLM calls — substring + FTS only;
  - per-session state only, keyed by ``session_id``;
  - the ring buffer is NEVER written to disk;
  - buffers expire after 30 minutes of inactivity.

Config (read defensively from ``.vouch/config.yaml``, like the rest of the
codebase — no pydantic Config model):
  - ``retrieval.reflex.enabled`` (default True)
  - ``retrieval.reflex.window`` (default 8)
  - ``retrieval.reflex.top_k`` (default 3)
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import index_db
from .storage import KBStore

DEFAULT_WINDOW = 8
DEFAULT_TOP_K = 3
_EXPIRY_SECONDS = 30 * 60


@dataclass
class _SessionBuffer:
    queries: deque[str]
    last_active: float = field(default_factory=time.monotonic)


# Module-level in-memory state. Keyed by session_id. Never persisted.
_BUFFERS: dict[str, _SessionBuffer] = {}


def _expire_stale(now: float) -> None:
    stale = [
        sid for sid, buf in _BUFFERS.items()
        if now - buf.last_active > _EXPIRY_SECONDS
    ]
    for sid in stale:
        del _BUFFERS[sid]


def record_query(session_id: str, query: str, *, window: int = DEFAULT_WINDOW) -> None:
    """Append ``query`` to this session's in-memory ring buffer."""
    query = (query or "").strip()
    if not session_id or not query:
        return
    now = time.monotonic()
    _expire_stale(now)
    window = max(1, window)
    buf = _BUFFERS.get(session_id)
    if buf is None or buf.queries.maxlen != window:
        existing = list(buf.queries) if buf is not None else []
        buf = _SessionBuffer(queries=deque(existing, maxlen=window))
        _BUFFERS[session_id] = buf
    buf.queries.append(query)
    buf.last_active = now


def reset_session(session_id: str) -> None:
    """Clear a session's buffer — called when the session ends."""
    _BUFFERS.pop(session_id, None)


def _buffered_queries(session_id: str) -> list[str]:
    now = time.monotonic()
    _expire_stale(now)
    buf = _BUFFERS.get(session_id)
    if buf is None:
        return []
    buf.last_active = now
    return list(buf.queries)


def _fts_entity_ids(store: KBStore, query: str) -> list[str]:
    """Entity ids the FTS index matches for ``query`` (best-effort)."""
    try:
        hits = index_db.search(store.kb_dir, query, limit=50)
    except sqlite3.Error:
        return []
    return [hid for kind, hid, _snip, _score in hits if kind == "entity"]


def _substring_entity_ids(entities: list[Any], query: str) -> list[str]:
    """Entity ids whose name or any alias appears in ``query`` (or vice versa)."""
    q = query.casefold()
    matched: list[str] = []
    for ent in entities:
        needles = [ent.name, *ent.aliases]
        for needle in needles:
            n = (needle or "").casefold()
            if n and (n in q or q in n):
                matched.append(ent.id)
                break
    return matched


def compute_salience(
    store: KBStore, session_id: str, *, top_k: int = DEFAULT_TOP_K
) -> list[dict]:
    """Rank buffered-query entity matches; return top-K salience records.

    Each record is ``{"entity_id", "claim_count", "top_claim_id"}`` where
    ``claim_count`` is the number of claims referencing the entity and
    ``top_claim_id`` is the highest-relevance claim (or None).
    """
    queries = _buffered_queries(session_id)
    if not queries:
        return []

    entities = store.list_entities()
    if not entities:
        return []
    by_id = {ent.id: ent for ent in entities}

    # Score entities by how many buffered queries match them (substring + FTS).
    scores: dict[str, int] = {}
    for query in queries:
        hits = set(_substring_entity_ids(entities, query))
        hits.update(eid for eid in _fts_entity_ids(store, query) if eid in by_id)
        for eid in hits:
            scores[eid] = scores.get(eid, 0) + 1

    if not scores:
        return []

    # Claims referencing each matched entity, by claim id (for stable picking).
    claims_by_entity: dict[str, list[str]] = {}
    for claim in store.list_claims():
        for eid in claim.entities:
            if eid in scores:
                claims_by_entity.setdefault(eid, []).append(claim.id)

    ranked = sorted(
        scores,
        key=lambda eid: (scores[eid], len(claims_by_entity.get(eid, [])), eid),
        reverse=True,
    )

    out: list[dict] = []
    for eid in ranked[: max(0, top_k)]:
        claim_ids = sorted(claims_by_entity.get(eid, []))
        out.append({
            "entity_id": eid,
            "claim_count": len(claim_ids),
            "top_claim_id": claim_ids[0] if claim_ids else None,
        })
    return out


def reflex_cfg(cfg: dict) -> tuple[bool, int, int]:
    """Read ``retrieval.reflex`` config defensively. Returns (enabled, window, top_k)."""
    retrieval = cfg.get("retrieval") if isinstance(cfg, dict) else None
    reflex = retrieval.get("reflex") if isinstance(retrieval, dict) else None
    if not isinstance(reflex, dict):
        reflex = {}

    enabled = reflex.get("enabled", True)
    enabled = bool(enabled) if isinstance(enabled, bool) else True

    window = reflex.get("window", DEFAULT_WINDOW)
    window = window if isinstance(window, int) and window > 0 else DEFAULT_WINDOW

    top_k = reflex.get("top_k", DEFAULT_TOP_K)
    top_k = top_k if isinstance(top_k, int) and top_k > 0 else DEFAULT_TOP_K

    return enabled, window, top_k


def attach_salience(
    result: dict, store: KBStore, session_id: str | None, cfg: dict
) -> dict:
    """Attach ``_meta.vouch_salience`` to ``result`` when the reflex applies.

    No-op (returns ``result`` unchanged, no field added) when the reflex is
    disabled, no ``session_id`` is given, or the session buffer is empty.
    """
    enabled, _window, top_k = reflex_cfg(cfg)
    if not enabled or not session_id:
        return result
    salience = compute_salience(store, session_id, top_k=top_k)
    if not salience:
        return result
    result.setdefault("_meta", {})["vouch_salience"] = salience
    return result
