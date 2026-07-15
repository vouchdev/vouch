"""Hot memory — session watch state and read-side response sidebars.

Two concerns live here:

1. **Session registry** — tracks what the active session is working on and the
   last relevance scores seen for approved claims. ``volunteer_context`` diffs
   snapshots to decide when a claim newly crosses the confidence threshold.

2. **Response sidebar** — inspired by gbrain's ``_meta.brain_hot_memory``
   pattern: read-side ``kb.*`` responses carry a small sidebar of recently
   approved claims so the agent doesn't re-query for "what just changed?"
   between turns. Strictly read-side; TTL-cached in-process.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .models import ClaimStatus, Entity, Page
from .storage import KBStore


@dataclass
class SalienceSnapshot:
    """Relevance scores for a single evaluation pass."""

    scores: dict[str, float] = field(default_factory=dict)


@dataclass
class HotMemory:
    """In-memory state for one active session watch."""

    session_id: str
    query: str
    agent: str
    project: str | None = None
    last_snapshot: SalienceSnapshot = field(default_factory=SalienceSnapshot)
    last_push_at: float | None = None
    push_count: int = 0
    volunteered: set[str] = field(default_factory=set)
    active: bool = True


_registry: dict[str, HotMemory] = {}
_lock = threading.Lock()


def register(
    *,
    session_id: str,
    query: str,
    agent: str,
    project: str | None = None,
) -> HotMemory:
    """Create or replace hot memory for *session_id*."""
    mem = HotMemory(
        session_id=session_id,
        query=query,
        agent=agent,
        project=project,
    )
    with _lock:
        _registry[session_id] = mem
    return mem


def get(session_id: str) -> HotMemory | None:
    with _lock:
        return _registry.get(session_id)


def unregister(session_id: str) -> None:
    with _lock:
        mem = _registry.pop(session_id, None)
        if mem is not None:
            mem.active = False


def update_snapshot(session_id: str, scores: dict[str, float]) -> SalienceSnapshot | None:
    """Store *scores* and return the previous snapshot (for delta detection)."""
    with _lock:
        mem = _registry.get(session_id)
        if mem is None:
            return None
        prev = mem.last_snapshot
        mem.last_snapshot = SalienceSnapshot(scores=dict(scores))
        return prev


def mark_volunteered(session_id: str, claim_id: str, *, pushed_at: float) -> None:
    with _lock:
        mem = _registry.get(session_id)
        if mem is None:
            return
        mem.volunteered.add(claim_id)
        mem.last_push_at = pushed_at
        mem.push_count += 1


# === response sidebar (gbrain ``_meta.brain_hot_memory`` pattern) =========


DEFAULT_TTL_SECONDS = 30.0
DEFAULT_LIMIT = 5
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600  # 1 week
TEXT_PREVIEW_CHARS = 200

LIST_ENVELOPE_DEPRECATION: dict[str, str] = {
    "message": (
        "kb.list_* responses now use a dict envelope with an items key; "
        "reading the flat list at result directly is deprecated"
    ),
    "migration": "use result.items instead of result when result is a list",
    "remove_in": "1.4.0",
}

# kb.* methods that attach ``_meta.vouch_hot_memory`` on read responses.
HOT_MEMORY_COVERED: frozenset[str] = frozenset({
    "kb.search",
    "kb.context",
    "kb.read_page",
    "kb.read_claim",
    "kb.read_entity",
    "kb.read_relation",
    "kb.list_pages",
    "kb.list_claims",
    "kb.list_entities",
    "kb.list_relations",
    "kb.list_sources",
    "kb.list_pending",
})

# Explicit exclusions for ``test_hot_memory_universal_coverage``.
HOT_MEMORY_EXCLUDED: dict[str, str] = {
    "kb.capabilities": "meta-tool — no KB payload to decorate",
    "kb.status": "meta-tool — health summary only",
    "kb.stats": "aggregates — sidebar would duplicate counts",
    "kb.digest": "aggregated reviewer briefing — sidebar would duplicate its own recency content",
    "kb.activity": "aggregated audit-log buckets — sidebar would duplicate counts",
    "kb.neighbors": "graph slice — out of scope for recency sidebar",
    "kb.synthesize": "answer-mode prose — sidebar adds noise",
    "kb.diff": "field-level revision diff — self-contained, not a claim browse",
    "kb.detect_themes": "cluster analysis — self-contained, not a claim browse",
    "kb.experts": "ranked entity analysis — self-contained, not a claim browse",
    "kb.triage_pending": (
        "advisory overlay on kb.list_pending — carries its own _meta.vouch_triage per item"
    ),
    "kb.list_skills": "skill/command catalogue — not a claim or artifact browse",
    "kb.get_skill": "skill/command catalogue — not a claim or artifact browse",
    "kb.list_sessions": "session pipeline rows — different shape from claim/page reads",
    "kb.session_transcript": "raw session transcript — different shape from claim reads",
    "kb.register_source": "write path — review gate",
    "kb.register_source_from_path": "write path — review gate",
    "kb.propose_claim": "write path — review gate",
    "kb.propose_page": "write path — review gate",
    "kb.propose_entity": "write path — review gate",
    "kb.propose_relation": "write path — review gate",
    "kb.propose_theme": "write path — review gate",
    "kb.propose_delete": "write path — review gate",
    "kb.compile": "write path — review gate (files page proposals via wiki-compiler)",
    "kb.summarize_session": "write path — review gate (files session-summary page proposals)",
    "kb.clear_claims": "lifecycle — mutates durable state",
    "kb.approve": "lifecycle — mutates durable state",
    "kb.reject": "lifecycle — mutates durable state",
    "kb.reject_extracted": "lifecycle — mutates durable state",
    "kb.expire": "lifecycle — mutates durable state",
    "kb.supersede": "lifecycle — mutates durable state",
    "kb.contradict": "lifecycle — mutates durable state",
    "kb.archive": "lifecycle — mutates durable state",
    "kb.confirm": "lifecycle — mutates durable state",
    "kb.cite": "lifecycle — mutates durable state",
    "kb.source_verify": "write path — verification intake",
    "kb.session_start": "session control — not a KB read",
    "kb.session_end": "session control — not a KB read",
    "kb.volunteer_context": "push channel — already surfaces hot claims",
    "kb.crystallize": "write path — proposal intake",
    "kb.index_rebuild": "maintenance — mutates derived index",
    "kb.lint": "diagnostics — no claim payload",
    "kb.doctor": "diagnostics — no claim payload",
    "kb.export": "bundle write — not a read response",
    "kb.export_check": "preflight — no claim sidebar needed",
    "kb.import_check": "preflight — no claim sidebar needed",
    "kb.audit": "event log — different shape from claim reads",
    "kb.reindex_embeddings": "maintenance — mutates derived index",
    "kb.dedup_scan": "analysis — not a standard read",
    "kb.eval_embeddings": "benchmark — not a standard read",
    "kb.embeddings_stats": "index stats — no claim payload",
    "kb.why": "provenance trace — self-contained",
    "kb.trace": "provenance trace — self-contained",
    "kb.impact": "graph impact — self-contained",
    "kb.graph_export": "bulk export — sidebar too large",
    "kb.provenance_rebuild": "maintenance — mutates derived state",
}


@dataclass(frozen=True)
class _CacheKey:
    kb_dir: str
    query_norm: str
    limit: int
    max_age_seconds: int


_SIDEBAR_CACHE: dict[_CacheKey, tuple[float, list[dict[str, Any]]]] = {}


def reset_sidebar_cache() -> None:
    """Drop every sidebar cache entry. Tests call this between cases."""
    _SIDEBAR_CACHE.clear()


def reset_cache() -> None:
    """Alias for tests that clear the sidebar TTL cache."""
    reset_sidebar_cache()


def _normalise_query(query: str | None) -> str:
    if not query:
        return ""
    return " ".join(query.lower().split())


def _preview(text: str) -> str:
    flat = " ".join(text.strip().split())
    if len(flat) <= TEXT_PREVIEW_CHARS:
        return flat
    return flat[: TEXT_PREVIEW_CHARS - 1] + "…"


def _is_active(status: ClaimStatus) -> bool:
    return status in {ClaimStatus.WORKING, ClaimStatus.STABLE, ClaimStatus.CONTESTED}


def query_bias_for_page(page: Page) -> str:
    """Bias hot-memory toward page title and tags."""
    return " ".join([page.title, *page.tags])


def query_bias_for_entity(entity: Entity) -> str:
    """Bias hot-memory toward entity name and aliases."""
    return " ".join([entity.name, *entity.aliases])


def compute_hot_memory(
    store: KBStore,
    *,
    query: str | None = None,
    limit: int = DEFAULT_LIMIT,
    exclude_ids: list[str] | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` recently-approved claims relevant to ``query``."""
    if limit <= 0:
        return []

    now = time.monotonic() if now is None else now
    query_norm = _normalise_query(query)
    key = _CacheKey(
        kb_dir=str(store.kb_dir),
        query_norm=query_norm,
        limit=limit,
        max_age_seconds=max_age_seconds,
    )
    cached = _SIDEBAR_CACHE.get(key)
    if cached is not None and (now - cached[0]) < ttl_seconds:
        rows = cached[1]
    else:
        rows = _compute_sidebar(store, query_norm, limit, max_age_seconds)
        _SIDEBAR_CACHE[key] = (now, rows)

    if not exclude_ids:
        return list(rows)
    excluded = set(exclude_ids)
    return [r for r in rows if r["id"] not in excluded]


def _matches_query(query_norm: str, text_lower: str) -> bool:
    if not query_norm:
        return False
    if query_norm in text_lower:
        return True
    return any(
        len(token) >= 3 and token in text_lower for token in query_norm.split()
    )


def _compute_sidebar(
    store: KBStore,
    query_norm: str,
    limit: int,
    max_age_seconds: int,
) -> list[dict[str, Any]]:
    from datetime import UTC, datetime

    try:
        claims = store.list_claims()
    except Exception:
        return []

    cutoff = datetime.now(UTC).timestamp() - max_age_seconds
    candidates: list[tuple[float, datetime, Any, str]] = []

    for c in claims:
        if not _is_active(c.status):
            continue
        ts_dt = c.last_confirmed_at or c.updated_at
        ts = ts_dt.timestamp()
        if ts < cutoff:
            continue
        text_lower = c.text.lower()
        matches_query = _matches_query(query_norm, text_lower)
        score = ts + (3600.0 if matches_query else 0.0)
        why = "recent+match" if matches_query else "recent"
        candidates.append((score, ts_dt, c, why))

    candidates.sort(key=lambda row: row[0], reverse=True)

    out: list[dict[str, Any]] = []
    for _score, ts_dt, c, why in candidates[:limit]:
        out.append({
            "id": c.id,
            "text": _preview(c.text),
            "type": c.type.value,
            "status": c.status.value,
            "citations": list(c.evidence),
            "approved_by": c.approved_by,
            "approved_at": ts_dt.isoformat(timespec="seconds"),
            "why_hot": why,
        })
    return out


def attach_hot_memory(
    result: Any,
    store: KBStore,
    *,
    query: str | None = None,
    limit: int = DEFAULT_LIMIT,
    exclude_ids: list[str] | None = None,
    list_envelope: bool = False,
) -> Any:
    """Attach ``_meta.vouch_hot_memory`` to *result* when the sidebar is non-empty.

    When *list_envelope* is true and *result* is a list, always wrap as
    ``{"items": [...], "_meta": {...}}`` and include a one-release-cycle
    deprecation note for JSONL clients that expected a flat list.
    """
    sidebar = compute_hot_memory(
        store, query=query, limit=limit, exclude_ids=exclude_ids,
    )

    if isinstance(result, list) and list_envelope:
        meta: dict[str, Any] = {"deprecation": dict(LIST_ENVELOPE_DEPRECATION)}
        if sidebar:
            meta["vouch_hot_memory"] = sidebar
        return {"items": result, "_meta": meta}

    if not sidebar:
        return result

    meta = {"vouch_hot_memory": sidebar}

    if isinstance(result, dict):
        existing_meta = result.get("_meta")
        if isinstance(existing_meta, dict):
            existing_meta.update(meta)
        else:
            result["_meta"] = meta
        return result

    if isinstance(result, list):
        return {"items": result, "_meta": meta}

    return result
