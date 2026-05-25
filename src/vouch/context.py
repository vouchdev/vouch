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

from . import index_db
from .models import ContextItem, ContextPack, ContextQuality
from .storage import ArtifactNotFoundError, KBStore

ContextItemKind = Literal["claim", "page", "entity", "relation", "source"]


def _retrieve(store: KBStore, query: str, limit: int
              ) -> list[tuple[str, str, str, float, str]]:
    """Return list of (kind, id, summary, score, backend).

    Dispatch order: embedding (semantic) -> FTS5 -> substring.
    """
    raw = index_db.search_semantic(store.kb_dir, query, limit=limit)
    if raw:
        return [(k, i, s, sc, "embedding") for k, i, s, sc in raw]
    try:
        hits = index_db.search(store.kb_dir, query, limit=limit)
        if hits:
            return [(k, i, s, sc, "fts5") for k, i, s, sc in hits]
    except sqlite3.Error:
        # FTS5 unavailable, db missing, or schema mismatch — fall through
        # to substring scan. Other exceptions are real bugs and propagate.
        pass
    return [
        (k, i, s, sc, "substring")
        for k, i, s, sc in store.search_substring(query, limit=limit)
    ]


def _citations_for_claim(store: KBStore, claim_id: str) -> list[str]:
    try:
        claim = store.get_claim(claim_id)
    except ArtifactNotFoundError:
        return []
    return list(claim.evidence)


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
) -> ContextPack | dict[str, Any]:
    hits = _retrieve(store, query, limit)
    items: list[ContextItem] = []
    for kind, hid, summary, score, backend in hits:
        cites: list[str] = []
        if kind == "claim":
            cites = _citations_for_claim(store, hid)
        summary = _enrich_summary(store, kind, hid, summary)
        items.append(
            ContextItem(
                id=hid, type=cast(ContextItemKind, kind), summary=summary, score=score,
                backend=backend, citations=cites,
                freshness="unknown",
            )
        )

    warnings: list[str] = []
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
    # Determine the backend used (all hits share the same backend in _retrieve).
    result["backend"] = hits[0][4] if hits else "none"
    if explain:
        result["explain"] = [
            {"kind": k, "id": i, "score": sc, "backend": hits[0][4] if hits else "none"}
            for k, i, _sn, sc, _be in hits
        ]
    return result
