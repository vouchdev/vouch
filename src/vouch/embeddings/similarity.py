"""Propose-time similarity warnings for claim proposals."""

from __future__ import annotations

import logging
from typing import Any

import yaml

from ..index_db import search_embedding
from ..models import ProposalKind, ProposalStatus
from ..storage import KBStore

# Match embeddings/dedup.py — do not import dedup here (it pulls numpy at import time).
DEFAULT_THRESHOLD = 0.95

_log = logging.getLogger("vouch.embeddings.similarity")

_MAX_WARNINGS_PER_CODE = 3
_SNIPPET_LEN = 120


def similarity_threshold(store: KBStore) -> float:
    """Resolve `review.similarity_threshold` from config, else dedup default."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            review = loaded.get("review")
            if isinstance(review, dict) and review.get("similarity_threshold") is not None:
                return float(review["similarity_threshold"])
    except Exception:
        pass
    return DEFAULT_THRESHOLD


def _snippet(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= _SNIPPET_LEN:
        return one_line
    return one_line[: _SNIPPET_LEN - 1] + "…"


def _claim_snippet(store: KBStore, claim_id: str) -> str:
    try:
        return _snippet(store.get_claim(claim_id).text)
    except Exception:
        return claim_id


def find_similar_on_propose(
    store: KBStore,
    text: str,
    *,
    exclude_claim_id: str | None = None,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Return non-blocking warnings for near-duplicate approved/pending claims."""
    stripped = text.strip()
    if not stripped:
        return []

    thresh = threshold if threshold is not None else similarity_threshold(store)

    try:
        from . import get_embedder

        embedder = get_embedder()
        query_vec = embedder.encode(stripped)
    except Exception as e:
        _log.debug("propose similarity skipped (no embedder): %s", e)
        return []

    warnings: list[dict[str, Any]] = []
    approved = _similar_approved(
        store, query_vec=query_vec, threshold=thresh,
        exclude_claim_id=exclude_claim_id,
    )
    warnings.extend(approved)

    pending = _similar_pending(
        store, query_vec=query_vec, embedder=embedder, threshold=thresh,
    )
    warnings.extend(pending)
    return warnings


def _similar_approved(
    store: KBStore,
    *,
    query_vec: Any,
    threshold: float,
    exclude_claim_id: str | None,
) -> list[dict[str, Any]]:
    # Fetch extra rows so filtering excluded ids still leaves enough matches.
    hits = search_embedding(
        store.kb_dir,
        query_vec=query_vec,
        kinds=("claim",),
        limit=_MAX_WARNINGS_PER_CODE + 2,
        min_score=threshold,
    )
    out: list[dict[str, Any]] = []
    for _kind, claim_id, _snip, cosine in hits:
        if claim_id == exclude_claim_id:
            continue
        out.append({
            "code": "similar_approved",
            "artifact_kind": "claim",
            "artifact_id": claim_id,
            "cosine": round(float(cosine), 4),
            "snippet": _claim_snippet(store, claim_id),
        })
        if len(out) >= _MAX_WARNINGS_PER_CODE:
            break
    return out


def _similar_pending(
    store: KBStore,
    *,
    query_vec: Any,
    embedder: Any,
    threshold: float,
) -> list[dict[str, Any]]:
    try:
        import numpy as np
    except ImportError:
        return []

    q = np.asarray(query_vec, dtype=np.float32)
    qnorm = float(np.linalg.norm(q))
    if qnorm > 0:
        q = q / qnorm

    scored: list[tuple[float, str, str]] = []
    for prop in store.list_proposals(ProposalStatus.PENDING):
        if prop.kind != ProposalKind.CLAIM:
            continue
        ptext = str(prop.payload.get("text", "")).strip()
        if not ptext:
            continue
        try:
            pvec = np.asarray(embedder.encode(ptext), dtype=np.float32)
        except Exception:
            continue
        pnorm = float(np.linalg.norm(pvec))
        if pnorm <= 0:
            continue
        cos = float(np.dot(q, pvec / pnorm))
        if cos >= threshold:
            scored.append((cos, prop.id, ptext))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "code": "similar_pending",
            "artifact_kind": "proposal",
            "artifact_id": prop_id,
            "cosine": round(cos, 4),
            "snippet": _snippet(ptext),
        }
        for cos, prop_id, ptext in scored[:_MAX_WARNINGS_PER_CODE]
    ]
