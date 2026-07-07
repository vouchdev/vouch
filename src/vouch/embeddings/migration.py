"""Model-identity migration and backfill."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import index_db
from .base import content_hash, get_embedder


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


def backfill_embeddings(store: Any, force: bool = False, stale: bool = False) -> dict[str, Any]:
    """Re-encode artifacts under the current adapter.

    With force=True, re-encode even when the stored content hash and model
    already match. With stale=True, scan every artifact but only call the
    embedder for missing, content-drifted, or model-drifted rows. If the stored
    KB embedding model differs from the current adapter, stale mode falls back
    to a full re-embed because hash equality is not safe across vector spaces.
    """
    if force and stale:
        raise ValueError("force and stale are mutually exclusive")
    embedder = get_embedder()
    mismatch = detect_mismatch(store.kb_dir)
    force_run = force or (stale and mismatch is not None)
    scanned = 0
    reembedded = 0
    skipped = 0
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
            text = text_of(obj)
            if not text or not text.strip():
                continue
            scanned += 1
            existing = index_db.get_embedding(store.kb_dir, kind=kind, id=obj.id)
            live_hash = content_hash(text)
            should_embed = (
                force_run
                or existing is None
                or existing[1] != live_hash
                or existing[2] != embedder.name
            )
            if should_embed:
                store._embed_and_store(kind=kind, id=obj.id, text=text, force=force_run)
                reembedded += 1
            else:
                skipped += 1
    index_db.set_embedding_meta(
        store.kb_dir, model=embedder.name, version=embedder.version, dim=embedder.dim,
    )
    return {
        "scanned": scanned,
        "reembedded": reembedded,
        "skipped": skipped,
        "model": embedder.name,
        "model_mismatch": mismatch,
    }
