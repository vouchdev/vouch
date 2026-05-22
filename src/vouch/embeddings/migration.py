"""Model-identity migration and backfill."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import index_db
from .base import get_embedder


def detect_mismatch(kb_dir: Path) -> dict[str, Any] | None:
    """Return mismatch info or None if no mismatch."""
    meta = index_db.get_embedding_meta(kb_dir)
    stored = meta.get("embedding_model")
    if not stored:
        return None
    try:
        current = get_embedder()
    except KeyError:
        return None
    if current.name == stored:
        return None
    return {
        "stored_model": stored,
        "stored_version": meta.get("embedding_model_version"),
        "stored_dim": meta.get("embedding_dim"),
        "current_model": current.name,
        "current_version": current.version,
        "current_dim": current.dim,
    }


def backfill_embeddings(store: Any) -> int:
    """Re-encode every artifact under the current adapter. Returns count touched."""
    embedder = get_embedder()
    touched = 0
    # (list-method, kind, text-extractor). Missing list methods are skipped;
    # errors raised inside the loop body propagate so a partial backfill never
    # silently updates embedding_meta as if it had succeeded.
    plan: list[tuple[str, str, Callable[[Any], str]]] = [
        ("list_claims", "claim", lambda c: c.text),
        ("list_pages", "page", lambda p: f"{p.title}\n\n{p.body}"),
        ("list_sources", "source", lambda s: s.title or s.locator or ""),
        ("list_entities", "entity", lambda e: f"{e.name}\n\n{e.description or ''}"),
        ("list_relations", "relation",
         lambda r: f"{r.source} {r.relation.value} {r.target}"),
        ("list_evidence", "evidence", lambda ev: ev.quote or ""),
    ]
    for list_name, kind, text_of in plan:
        lister = getattr(store, list_name, None)
        if lister is None:
            continue
        for obj in lister():
            store._embed_and_store(kind=kind, id=obj.id, text=text_of(obj))
            touched += 1
    index_db.set_embedding_meta(
        store.kb_dir, model=embedder.name, version=embedder.version, dim=embedder.dim,
    )
    return touched
