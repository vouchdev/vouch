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

    if require_citations:
        for it in items:
            if it.type == "claim" and not it.citations:
                uncited.append(it.id)

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
    if explain:
        result["explain"] = [
            {"kind": k, "id": i, "score": sc, "backend": hits[0][4] if hits else "none"}
            for k, i, _sn, sc, _be in hits
        ]
    return result
