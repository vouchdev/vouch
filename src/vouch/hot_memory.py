"""Hot-memory injection — top-N recently-approved claims as a response sidebar.

Inspired by gbrain's ``_meta.brain_hot_memory`` pattern: every relevant
``kb.*`` read carries a small sidebar of recently-approved claims so the
calling agent doesn't have to re-query for "what just changed in this KB?"
between turns.

* Strictly read-side — never affects on-disk state.
* TTL-cached in-process (30s default) keyed by ``(kb_dir, query, limit)``
  so repeated tool calls inside a single agent turn don't re-scan the
  whole claims directory.
* Bias by ``query`` substring when supplied — the search/context flows
  benefit from "recently approved claims that mention these words"; the
  open-ended reads (``read_claim``, ``list_pending``) get plain recency.
* Deduped against caller-supplied ids so search results aren't echoed
  back in the sidebar.
* Defensive: a malformed claim file never breaks the response — the
  sidebar is best-effort and silently skips bad rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .models import ClaimStatus
from .storage import KBStore

DEFAULT_TTL_SECONDS = 30.0
DEFAULT_LIMIT = 5
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600  # 1 week
TEXT_PREVIEW_CHARS = 200


@dataclass(frozen=True)
class _CacheKey:
    kb_dir: str
    query_norm: str
    limit: int
    max_age_seconds: int


# Single in-process cache. Tests can reset it via reset_cache().
_CACHE: dict[_CacheKey, tuple[float, list[dict[str, Any]]]] = {}


def reset_cache() -> None:
    """Drop every cached entry. Tests call this between cases."""
    _CACHE.clear()


def _normalise_query(query: str | None) -> str:
    if not query:
        return ""
    return " ".join(query.lower().split())


def _preview(text: str) -> str:
    """Trim claim text to a one-shot agent-friendly preview."""
    flat = " ".join(text.strip().split())
    if len(flat) <= TEXT_PREVIEW_CHARS:
        return flat
    return flat[: TEXT_PREVIEW_CHARS - 1] + "…"


def _is_active(status: ClaimStatus) -> bool:
    """A claim is hot only if it's still standing — superseded / archived
    claims have been demoted and would confuse the agent."""
    return status in {ClaimStatus.WORKING, ClaimStatus.STABLE,
                      ClaimStatus.CONTESTED}


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
    """Return up to ``limit`` recently-approved claims relevant to ``query``.

    Each entry has the same shape across callers:

    ``{id, text, type, status, citations, approved_by, approved_at, why_hot}``

    ``why_hot`` is a short string for debuggability — ``recent``,
    ``recent+match:<q>``, etc.
    """
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
    cached = _CACHE.get(key)
    if cached is not None and (now - cached[0]) < ttl_seconds:
        rows = cached[1]
    else:
        rows = _compute(store, query_norm, limit, max_age_seconds)
        _CACHE[key] = (now, rows)

    if not exclude_ids:
        return list(rows)
    excluded = set(exclude_ids)
    return [r for r in rows if r["id"] not in excluded]


def _compute(
    store: KBStore,
    query_norm: str,
    limit: int,
    max_age_seconds: int,
) -> list[dict[str, Any]]:
    """Inner pass that actually walks the claims directory. Cache-miss path."""
    try:
        claims = store.list_claims()
    except Exception:
        # KB is malformed or partially written — degrade to empty sidebar.
        return []

    # Recency cutoff is wall-clock (claims carry datetimes), not monotonic.
    from datetime import UTC, datetime
    cutoff = datetime.now(UTC).timestamp() - max_age_seconds

    candidates = []
    for c in claims:
        if not _is_active(c.status):
            continue
        # Prefer the explicit review-gate timestamp; fall back to updated_at
        # so a freshly approved claim still surfaces even if last_confirmed_at
        # hasn't been set yet.
        ts_dt = c.last_confirmed_at or c.updated_at
        ts = ts_dt.timestamp()
        if ts < cutoff:
            continue
        text_lower = c.text.lower()
        matches_query = bool(query_norm) and query_norm in text_lower
        # Score: recency dominates; query match is a fixed boost so a
        # mention-bearing claim from yesterday outranks an off-topic one
        # from an hour ago.
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
) -> Any:
    """Attach an ``_meta.vouch_hot_memory`` sidebar to ``result`` in place.

    Behaviour by shape:
    * ``result`` is a ``dict`` → set ``result["_meta"]["vouch_hot_memory"]``.
    * ``result`` is a ``list`` → wrap as ``{"items": result, "_meta": {...}}``
      so the caller can keep iterating ``items`` while ``_meta`` is sidecar.
    * anything else → return unchanged (no envelope to attach to).

    The sidebar is omitted entirely when empty so consumers don't see a
    spurious key for KBs that have no recent activity.
    """
    sidebar = compute_hot_memory(
        store, query=query, limit=limit, exclude_ids=exclude_ids,
    )
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
