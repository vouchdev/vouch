"""Search diagnostics for explaining retrieval misses."""

from __future__ import annotations

from typing import Any

from . import index_db
from .embeddings.fusion import rrf_fuse
from .scoping import filter_hits, scoped_fetch_limit, viewer_from
from .storage import KBStore

Hit = tuple[str, str, str, float]

_VALID_BACKENDS = {"auto", "embedding", "fts5", "substring", "hybrid"}


def _artifact_exists(store: KBStore, kind: str, artifact_id: str) -> bool:
    if kind == "claim":
        return store._claim_path(artifact_id).exists()
    if kind == "page":
        return store._page_path(artifact_id).exists()
    if kind == "entity":
        return store._entity_path(artifact_id).exists()
    if kind == "source":
        return (store._source_dir(artifact_id) / "meta.yaml").exists()
    if kind == "relation":
        return store._relation_path(artifact_id).exists()
    if kind == "evidence":
        return store._evidence_path(artifact_id).exists()
    return False


def _indexed(store: KBStore, kind: str, artifact_id: str) -> bool:
    table = {"claim": "claims_fts", "page": "pages_fts", "entity": "entities_fts"}.get(kind)
    if table is not None:
        with index_db.open_db(store.kb_dir) as conn:
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1",
                (artifact_id,),
            ).fetchone()
        return row is not None
    return index_db.get_embedding(store.kb_dir, kind=kind, id=artifact_id) is not None


def _rank(hits: list[Hit], kind: str, artifact_id: str) -> int | None:
    for idx, (hit_kind, hit_id, _, _) in enumerate(hits, start=1):
        if hit_kind == kind and hit_id == artifact_id:
            return idx
    return None


def _search_backend(
    store: KBStore,
    query: str,
    *,
    backend: str,
    limit: int,
    min_score: float,
) -> tuple[str, list[Hit], list[str]]:
    warnings: list[str] = []
    hits: list[Hit] = []
    used = backend

    if backend in ("auto", "embedding"):
        hits = index_db.search_semantic(store.kb_dir, query, limit=limit, min_score=min_score)
        if hits:
            return "embedding", hits, warnings
        if backend == "embedding":
            return "embedding", [], warnings

    if backend in ("auto", "fts5"):
        try:
            hits = index_db.search(store.kb_dir, query, limit=limit)
        except Exception as exc:
            warnings.append(f"fts5 search failed: {exc}")
            hits = []
        if hits:
            return "fts5", hits, warnings
        if backend == "fts5":
            return "fts5", [], warnings

    if backend in ("auto", "substring"):
        return "substring", store.search_substring(query, limit=limit), warnings

    if backend == "hybrid":
        emb = index_db.search_semantic(store.kb_dir, query, limit=limit * 2, min_score=min_score)
        try:
            fts = index_db.search(store.kb_dir, query, limit=limit * 2)
        except Exception as exc:
            warnings.append(f"fts5 search failed: {exc}")
            fts = []
        return "hybrid", rrf_fuse(emb, fts, limit=limit), warnings

    return used, hits, warnings


def diagnose_search(
    store: KBStore,
    *,
    query: str,
    target_kind: str,
    target_id: str,
    limit: int = 10,
    backend: str = "auto",
    min_score: float = 0.0,
    project: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Explain whether a target artifact appears in search results."""
    if backend not in _VALID_BACKENDS:
        raise ValueError(f"unknown backend: {backend!r}")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    viewer = viewer_from(config_path=store.config_path, project=project, agent=agent)
    fetch_limit = scoped_fetch_limit(limit, viewer)
    used, raw_hits, warnings = _search_backend(
        store,
        query,
        backend=backend,
        limit=fetch_limit,
        min_score=min_score,
    )
    scoped = filter_hits(store, raw_hits, viewer, limit=limit)
    raw_rank = _rank(raw_hits, target_kind, target_id)
    scoped_rank = _rank(scoped, target_kind, target_id)

    reasons: list[str] = []
    exists = _artifact_exists(store, target_kind, target_id)
    indexed = _indexed(store, target_kind, target_id) if exists else False
    if not exists:
        reasons.append("target artifact does not exist")
    elif not indexed:
        reasons.append("target artifact is not present in the derived index")
    elif raw_rank is None:
        reasons.append("target did not match the raw backend result window")
    elif scoped_rank is None:
        reasons.append("target matched before scope filtering but was hidden from this viewer")
    else:
        reasons.append("target is present in scoped results")

    return {
        "query": query,
        "backend": used,
        "viewer": {"project": viewer.project, "agent": viewer.agent},
        "target": {
            "kind": target_kind,
            "id": target_id,
            "exists": exists,
            "indexed": indexed,
            "raw_rank": raw_rank,
            "scoped_rank": scoped_rank,
            "found": scoped_rank is not None,
        },
        "limit": limit,
        "fetch_limit": fetch_limit,
        "reasons": reasons,
        "warnings": warnings,
        "hits": [
            {"kind": k, "id": i, "snippet": sn, "score": sc, "backend": used}
            for k, i, sn, sc in scoped
        ],
    }
