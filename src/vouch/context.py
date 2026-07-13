"""Context-pack assembly — `vouch context` / `kb_context`.

A ContextPack is the bundle an agent gets back when it asks "what does the
KB know that's relevant to <task>". It's the shape AKBP defines so that
hosts can compare ranking quality and budget enforcement consistently.

This implementation:
  - runs FTS5 search if state.db has any rows, falls back to substring scan
  - resolves citations for every claim hit
  - enforces a `max_chars` budget by clipping summaries before omitting items
  - flags freshness using the source-verification cache (when available)
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal, cast

import yaml

from . import graph, index_db
from .embeddings.fusion import rrf_fuse
from .models import ClaimStatus, ContextItem, ContextPack, ContextQuality
from .scoping import (
    ViewerContext,
    filter_hits,
    scoped_fetch_limit,
    viewer_from,
)
from .storage import ArtifactNotFoundError, KBStore

# Claim statuses that have been explicitly retracted from active circulation.
# Any retrieval surface that hands knowledge back to an agent must exclude
# these — otherwise the archive/supersede/redact controls are decorative.
# CONTESTED is intentionally not in this set: contested claims are still
# part of the conversation, just disputed; lint / context callers can
# decide what to do with them.
_RETRACTED_CLAIM_STATUSES = frozenset({
    ClaimStatus.ARCHIVED,
    ClaimStatus.SUPERSEDED,
    ClaimStatus.REDACTED,
})

ContextItemKind = Literal["claim", "page", "entity", "relation", "source"]

_VALID_BACKENDS = ("auto", "hybrid", "embedding", "fts5", "substring")
_RERANKER_CACHE: Any | None = None


def _configured_backend(store: KBStore) -> str:
    """Resolve the retrieval backend from `config.yaml`, defaulting to "auto".

    Reads the singular `retrieval.backend` string. For KBs initialised
    before this knob existed, a legacy `retrieval.backends` list is honoured
    by taking its first recognised entry. Anything unreadable or unrecognised
    falls back to "auto".
    """
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return "auto"
    if not isinstance(loaded, dict):
        return "auto"
    retrieval = loaded.get("retrieval")
    if not isinstance(retrieval, dict):
        return "auto"
    backend = retrieval.get("backend")
    if isinstance(backend, str) and backend in _VALID_BACKENDS:
        return backend
    legacy = retrieval.get("backends")
    if isinstance(legacy, list):
        for entry in legacy:
            if isinstance(entry, str) and entry in _VALID_BACKENDS:
                return entry
    return "auto"


def _configured_rerank(store: KBStore, *, limit: int) -> tuple[bool, int]:
    """Resolve the optional context rerank stage from config.yaml.

    Defaults to disabled so existing KBs keep byte-identical ordering unless
    they opt in with ``retrieval.rerank.enabled: true``. ``top_k`` is the
    window to reorder; by default it is the caller's context limit.
    """
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False, limit
    if not isinstance(loaded, dict):
        return False, limit
    retrieval = loaded.get("retrieval")
    if not isinstance(retrieval, dict):
        return False, limit
    rerank = retrieval.get("rerank")
    if not isinstance(rerank, dict):
        return False, limit

    enabled = rerank.get("enabled", False)
    enabled = enabled if isinstance(enabled, bool) else False

    top_k = rerank.get("top_k", limit)
    top_k = (
        top_k
        if isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0
        else limit
    )
    return enabled, top_k


def _configured_recency(store: KBStore) -> tuple[bool, float]:
    """Resolve the optional recency-decay stage from config.yaml.

    Defaults to disabled so existing KBs keep byte-identical ordering unless
    they opt in with ``retrieval.recency.enabled: true`` (new KBs get it from
    the starter config). ``half_life_days`` is the age at which an artifact's
    score contribution halves; <= 0 falls back to the 90-day default.
    """
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False, 90.0
    if not isinstance(loaded, dict):
        return False, 90.0
    retrieval = loaded.get("retrieval")
    if not isinstance(retrieval, dict):
        return False, 90.0
    recency = retrieval.get("recency")
    if not isinstance(recency, dict):
        return False, 90.0

    enabled = recency.get("enabled", False)
    enabled = enabled if isinstance(enabled, bool) else False

    half_life = recency.get("half_life_days", 90.0)
    half_life = (
        float(half_life)
        if isinstance(half_life, (int, float)) and not isinstance(half_life, bool)
        and half_life > 0
        else 90.0
    )
    return enabled, half_life


def _artifact_timestamp(store: KBStore, kind: str, artifact_id: str) -> datetime | None:
    try:
        if kind == "claim":
            claim = store.get_claim(artifact_id)
            return claim.updated_at or claim.created_at
        if kind == "page":
            page = store.get_page(artifact_id)
            return page.updated_at or page.created_at
        if kind == "entity":
            entity = store.get_entity(artifact_id)
            return entity.updated_at or entity.created_at
    except (ArtifactNotFoundError, OSError):
        return None
    return None


def _maybe_recency(
    store: KBStore,
    *,
    hits: list[tuple[str, str, str, float]],
) -> list[tuple[str, str, str, float]]:
    """Blend a recency half-life decay into hit scores, newest-favouring.

    Rescoring-only: ``score * (0.5 + 0.5 * decay)`` keeps every hit in the
    set (an old artifact loses at most half its score, it never vanishes),
    and artifacts with no readable timestamp are left at full weight.
    """
    enabled, half_life_days = _configured_recency(store)
    if not enabled or not hits:
        return hits
    now = datetime.now(UTC)
    rescored: list[tuple[str, str, str, float]] = []
    for kind, artifact_id, summary, score in hits:
        ts = _artifact_timestamp(store, kind, artifact_id)
        if ts is None:
            rescored.append((kind, artifact_id, summary, score))
            continue
        # Whole days only: sub-day age is noise at a 90-day half-life, and
        # quantizing keeps repeat queries byte-identical within a day
        # (fresh artifacts decay 1.0, so same-day scores never drift).
        age_days = float(int(max((now - ts).total_seconds() / 86400.0, 0.0)))
        decay = 0.5 ** (age_days / half_life_days)
        rescored.append((kind, artifact_id, summary, score * (0.5 + 0.5 * decay)))
    rescored.sort(key=lambda h: h[3], reverse=True)
    return rescored


def _default_reranker_cached() -> Any:
    global _RERANKER_CACHE
    if _RERANKER_CACHE is None:
        from .embeddings.rerank import default_reranker

        _RERANKER_CACHE = default_reranker()
    return _RERANKER_CACHE


def _maybe_rerank(
    store: KBStore,
    *,
    query: str,
    hits: list[tuple[str, str, str, float]],
    limit: int,
) -> list[tuple[str, str, str, float]]:
    enabled, top_k = _configured_rerank(store, limit=limit)
    if not enabled or not hits or top_k <= 0:
        return hits

    window_size = min(top_k, len(hits))
    window = hits[:window_size]
    try:
        from .embeddings.rerank import rerank as do_rerank

        reranked = do_rerank(
            query=query,
            hits=window,
            reranker=_default_reranker_cached(),
            top_k=window_size,
        )
    except ImportError:
        return hits

    # Keep reranking as an ordering-only stage: the configured window may move,
    # but it must not add/drop artifacts from the already-scoped result set.
    original_by_key = {(hit[0], hit[1]): hit for hit in window}
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str, str, float]] = []
    for hit in reranked:
        key = (hit[0], hit[1])
        if key in original_by_key and key not in seen:
            ordered.append(original_by_key[key])
            seen.add(key)
    for hit in window:
        key = (hit[0], hit[1])
        if key not in seen:
            ordered.append(hit)
            seen.add(key)
    return ordered + hits[window_size:]


def _retrieve(
    store: KBStore,
    query: str,
    limit: int,
    viewer: ViewerContext,
) -> list[tuple[str, str, str, float, str]]:
    """Return list of (kind, id, summary, score, backend).

    The backend is chosen by `retrieval.backend` in config.yaml:
      - "auto" (default) / "hybrid": fuse embedding + FTS5 via RRF, falling
        back to a substring scan only if both retrievers are empty
      - "embedding": semantic search only
      - "fts5": lexical FTS5 only
      - "substring": substring scan only
    """
    backend = _configured_backend(store)
    fetch_limit = scoped_fetch_limit(limit, viewer)

    if backend in ("auto", "hybrid"):
        sem = index_db.search_semantic(store.kb_dir, query, limit=fetch_limit)
        try:
            lex = index_db.search(store.kb_dir, query, limit=fetch_limit)
        except sqlite3.Error:
            lex = []
        fused = rrf_fuse(sem, lex, limit=fetch_limit)
        if fused:
            filtered = filter_hits(store, fused, viewer, limit=limit)
            filtered = _maybe_recency(store, hits=filtered)
            filtered = _maybe_rerank(store, query=query, hits=filtered, limit=limit)
            return [(k, i, s, sc, "hybrid") for k, i, s, sc in filtered]
        # both retrievers empty -> fall through to the substring scan below.

    if backend == "embedding":
        raw = index_db.search_semantic(store.kb_dir, query, limit=fetch_limit)
        if raw:
            filtered = filter_hits(store, raw, viewer, limit=limit)
            return [(k, i, s, sc, "embedding") for k, i, s, sc in filtered]
        return []

    if backend == "fts5":
        try:
            hits = index_db.search(store.kb_dir, query, limit=fetch_limit)
            if hits:
                filtered = filter_hits(store, hits, viewer, limit=limit)
                return [(k, i, s, sc, "fts5") for k, i, s, sc in filtered]
        except sqlite3.Error:
            pass
        return []

    substring_hits = store.search_substring(query, limit=fetch_limit)
    filtered = filter_hits(store, substring_hits, viewer, limit=limit)
    return [(k, i, s, sc, "substring") for k, i, s, sc in filtered]


def search_kb(
    store: KBStore,
    *,
    query: str,
    limit: int = 10,
    backend: str | None = None,
    min_score: float = 0.0,
    project: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """The one `kb.search` implementation every surface delegates to.

    MCP, JSONL, and the CLI used to carry three copies of the backend
    waterfall and drifted (fusion landed in one, not the others). Keep the
    logic here only.

    ``backend=None`` defers to ``retrieval.backend`` in config.yaml; "auto"
    then fuses embedding + FTS5 via RRF and falls back to a substring scan
    only when both are empty. The ``retrieval`` block reports what actually
    served the query — a base install degrades to "fts5" and says so.
    """
    backend_arg = backend or _configured_backend(store)
    viewer = viewer_from(
        config_path=store.config_path,
        project=project,
        agent=agent,
    )
    fetch_limit = scoped_fetch_limit(limit, viewer)
    hits: list[tuple[str, str, str, float]] = []
    used = backend_arg

    valid_backends = {"auto", "embedding", "fts5", "substring", "hybrid"}
    if backend_arg not in valid_backends:
        raise ValueError(
            f"unknown backend: {backend_arg!r} "
            f"(expected one of {sorted(valid_backends)})"
        )

    if backend_arg in ("auto", "hybrid"):
        emb = index_db.search_semantic(
            store.kb_dir, query, limit=fetch_limit * 2, min_score=min_score,
        )
        try:
            fts = index_db.search(store.kb_dir, query, limit=fetch_limit * 2)
        except sqlite3.Error:
            fts = []
        hits = rrf_fuse(emb, fts, limit=fetch_limit)
        if emb and fts:
            used = "hybrid"
        elif emb:
            used = "embedding"
        elif fts:
            used = "fts5"
        if not hits and backend_arg == "auto":
            hits = store.search_substring(query, limit=fetch_limit)
            used = "substring"
    elif backend_arg == "embedding":
        hits = index_db.search_semantic(
            store.kb_dir, query, limit=fetch_limit, min_score=min_score,
        )
        used = "embedding"
    elif backend_arg == "fts5":
        try:
            hits = index_db.search(store.kb_dir, query, limit=fetch_limit)
        except sqlite3.Error:
            hits = []
        used = "fts5"
    else:  # substring
        hits = store.search_substring(query, limit=fetch_limit)
        used = "substring"

    semantic_ok = index_db.semantic_search_available()
    scoped = filter_hits(store, hits, viewer, limit=limit)
    return {
        "backend": used,
        "retrieval": {
            "configured": backend_arg,
            "used": used,
            "semantic_available": semantic_ok,
            "degraded": (
                backend_arg in ("auto", "hybrid", "embedding")
                and not semantic_ok
            ),
        },
        "viewer": {"project": viewer.project, "agent": viewer.agent},
        "hits": [
            {"kind": k, "id": i, "snippet": sn, "score": sc, "backend": used}
            for k, i, sn, sc in scoped
        ],
    }


def _enrich_summary(store: KBStore, kind: str, artifact_id: str, summary: str) -> str:
    """Return a non-empty summary, falling back to the stored artifact text."""
    if summary:
        return summary
    try:
        if kind == "claim":
            return store.get_claim(artifact_id).text
        if kind == "page":
            p = store.get_page(artifact_id)
            return p.title or p.body[:200]
        if kind == "entity":
            e = store.get_entity(artifact_id)
            return e.name or (e.description or "")[:200]
    except Exception:
        pass
    return summary


def _append_graph_neighbors(
    store: KBStore,
    items: list[ContextItem],
    *,
    depth: int,
    limit: int,
    rel_types: list[str] | None,
) -> list[str]:
    """Expand `items` with 1-hop (or deeper) graph neighbors. Returns warnings."""
    warnings: list[str] = []
    if not items:
        return warnings
    seed_scores = {it.id: it.score for it in items}
    neighbors = graph.graph_neighbors_for_seeds(
        store,
        [it.id for it in items],
        depth=depth,
        rel_types=rel_types,
        max_nodes=limit,
    )
    existing = {it.id for it in items}
    added = 0
    for node in neighbors:
        nid = node["id"]
        if nid in existing:
            continue
        kind = node["kind"]
        cites: list[str] = []
        if kind == "claim":
            try:
                claim = store.get_claim(nid)
            except ArtifactNotFoundError:
                continue
            if claim.status in _RETRACTED_CLAIM_STATUSES:
                continue
            cites = list(claim.evidence)
        via = node.get("via", "")
        parent_score = seed_scores.get(via, 0.5)
        distance = int(node.get("distance", 1))
        score = parent_score * (0.8 ** distance)
        summary = node.get("summary") or _enrich_summary(store, kind, nid, "")
        items.append(
            ContextItem(
                id=nid,
                type=cast(ContextItemKind, kind),
                summary=summary,
                score=score,
                backend="graph",
                citations=cites,
                freshness="unknown",
            )
        )
        existing.add(nid)
        added += 1
    if added:
        warnings.append(f"graph expansion added {added} neighbor(s)")
    return warnings


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dedupe_near_duplicates(items: list[ContextItem]) -> list[ContextItem]:
    """Drop items whose summary is near-identical to a higher-scored one.

    The *keep* decision runs in descending-score order so the highest-scored
    member of a near-duplicate cluster survives; survivors are returned in the
    caller's original order. build_context_pack appends lower-priority items
    (graph-expansion neighbours) after the ranked hits and relies on that tail
    ordering for budget eviction, so this pass must not re-rank the pack.

    Cheap greedy heuristic (token-set Jaccard >= 0.85 over the first 40 tokens);
    it can over-merge long near-templated claims that differ by a single token.
    """
    dropped: set[int] = set()
    kept_tokens: list[set[str]] = []
    order = sorted(range(len(items)), key=lambda i: items[i].score, reverse=True)
    for idx in order:
        toks = set(items[idx].summary.lower().split()[:40])
        if any(_jaccard(toks, seen) >= 0.85 for seen in kept_tokens):
            dropped.add(idx)
            continue
        kept_tokens.append(toks)
    return [it for i, it in enumerate(items) if i not in dropped]


def build_context_pack(
    store: KBStore,
    *,
    query: str,
    limit: int = 10,
    max_chars: int | None = None,
    min_items: int = 0,
    require_citations: bool = False,
    fail_on_warnings: bool = False,
    fail_on_budget_truncation: bool = False,
    explain: bool = False,
    project: str | None = None,
    agent: str | None = None,
    expand_graph: bool = False,
    graph_depth: int = 1,
    graph_limit: int = 20,
    graph_rel_types: list[str] | None = None,
) -> ContextPack | dict[str, Any]:
    viewer = viewer_from(
        config_path=store.config_path,
        project=project,
        agent=agent,
    )
    hits = _retrieve(store, query, limit, viewer)
    items: list[ContextItem] = []
    for kind, hid, summary, score, backend in hits:
        cites: list[str] = []
        if kind == "claim":
            # Exclude retracted claims even if the underlying index still
            # matches them (the FTS5 row's status column can lag — see #78
            # and the companion update_claim reindex). A missing claim is
            # also treated as retracted: the YAML may have been deleted
            # while the index row survived.
            try:
                claim = store.get_claim(hid)
            except ArtifactNotFoundError:
                continue
            if claim.status in _RETRACTED_CLAIM_STATUSES:
                continue
            cites = list(claim.evidence)
        summary = _enrich_summary(store, kind, hid, summary)
        items.append(
            ContextItem(
                id=hid, type=cast(ContextItemKind, kind), summary=summary, score=score,
                backend=backend, citations=cites,
                freshness="unknown",
            )
        )

    warnings: list[str] = []
    if expand_graph:
        warnings.extend(
            _append_graph_neighbors(
                store, items, depth=graph_depth, limit=graph_limit,
                rel_types=graph_rel_types,
            )
        )

    items = _dedupe_near_duplicates(items)

    failed: list[str] = []
    uncited: list[str] = []
    budget_truncated = False
    budget_clipped = 0
    budget_omitted = 0

    if max_chars is not None:
        total = sum(len(i.summary) for i in items)
        if total > max_chars:
            budget_truncated = True
            # First clip each summary uniformly, then drop tail items if still over.
            for it in items:
                if len(it.summary) > 200:
                    it.summary = it.summary[:200] + "…"
                    budget_clipped += 1
            while items and sum(len(i.summary) for i in items) > max_chars:
                items.pop()
                budget_omitted += 1

    # Compute the citation gate over the items actually returned — after the
    # max_chars budget has dropped tail items — so the gate never fails on (or
    # reports in uncited_items) claims the consumer did not receive.
    if require_citations:
        uncited = [
            it.id for it in items if it.type == "claim" and not it.citations
        ]

    if len(items) < min_items:
        warnings.append(f"only {len(items)} items, minimum {min_items}")
        failed.append("min_items")
    if uncited:
        warnings.append(f"{len(uncited)} uncited claims")
        if require_citations:
            failed.append("require_citations")
    if fail_on_budget_truncation and budget_truncated:
        failed.append("budget_truncated")
    if fail_on_warnings and warnings:
        failed.append("fail_on_warnings")

    quality = ContextQuality(
        ok=len(failed) == 0,
        minimum_items=min_items,
        require_citations=require_citations,
        fail_on_warnings=fail_on_warnings,
        budget_truncated=budget_truncated,
        budget_omitted_items=budget_omitted,
        budget_clipped_items=budget_clipped,
        items=len(items),
        uncited_items=uncited,
        warnings=len(warnings),
        failed=failed,
    )

    pack = ContextPack(query=query, items=items, quality=quality, warnings=warnings)
    result: dict[str, Any] = pack.model_dump()
    result["viewer"] = {
        "project": viewer.project,
        "agent": viewer.agent,
    }
    # Determine the backend used (all hits share the same backend in _retrieve).
    result["backend"] = hits[0][4] if hits else "none"
    # Honesty block: say when a semantic-capable backend actually served
    # lexical-only results (embeddings extra absent / no embedder registered)
    # instead of letting "hybrid" imply semantic coverage that never happened.
    configured = _configured_backend(store)
    semantic_ok = index_db.semantic_search_available()
    recency_enabled, _ = _configured_recency(store)
    result["retrieval"] = {
        "configured": configured,
        "used": result["backend"],
        "semantic_available": semantic_ok,
        "degraded": (
            configured in ("auto", "hybrid", "embedding") and not semantic_ok
        ),
        "recency": recency_enabled,
    }
    if explain:
        result["explain"] = [
            {"kind": k, "id": i, "score": sc, "backend": hits[0][4] if hits else "none"}
            for k, i, _sn, sc, _be in hits
        ]
    return result
