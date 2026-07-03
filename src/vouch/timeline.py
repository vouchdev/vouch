"""``kb.timeline`` — chronological trajectory of an entity (vouchdev/vouch#313).

``kb.read_entity`` returns an entity and its claims as a flat set; ``kb.neighbors``
expands graph adjacency at a point in time. Neither answers "what did the KB
learn about this entity, *in what order*?" — a trajectory. The raw material is
already on disk: ``Claim`` and ``Relation`` carry ``created_at``, and decision
time is recoverable from the append-only ``audit.log.jsonl``. This orders an
entity's approved claims and relations along that time axis.

Pure read. It never proposes, approves, or writes — a viewport over
already-reviewed artifacts, exactly like ``read_entity`` / ``neighbors``. Only
*approved* durable artifacts are read (``list_claims`` / ``list_relations``);
pending proposals never appear. All ordering logic lives here, not in
``storage.py``, which stays pure I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .audit import read_events
from .metrics import APPROVE_RE
from .models import Claim, Relation
from .storage import KBStore

# Ordering axes. ``effective`` orders by the artifact's own ``created_at`` (when
# the fact entered the KB); ``decided`` orders by the moment the proposal that
# produced it was approved, recovered from the audit log.
ORDER_EFFECTIVE = "effective"
ORDER_DECIDED = "decided"
ORDERS = (ORDER_EFFECTIVE, ORDER_DECIDED)


class TimelineError(ValueError):
    """User-visible bad input (e.g. an unknown ``order``)."""


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _decided_map(store: KBStore) -> dict[str, datetime]:
    """artifact id -> approval time, from the authoritative audit stream.

    ``proposals.approve`` logs an approve event with ``object_ids = [proposal_id,
    result_id]``; the event's ``created_at`` is the decision time. Artifacts
    written outside the proposal path (rare, e.g. a seeded starter KB) simply
    have no entry and fall back to their ``created_at``.
    """
    out: dict[str, datetime] = {}
    for ev in read_events(store.kb_dir):
        if APPROVE_RE.match(ev.event) and len(ev.object_ids) >= 2:
            ts = _as_utc(ev.created_at)
            if ts is not None:
                out[ev.object_ids[1]] = ts
    return out


def _claim_summary(c: Claim) -> str:
    return str(c.text).strip()[:120] or "—"


def _relation_summary(r: Relation) -> str:
    return f"{r.source} {r.relation.value} {r.target}"


def build_timeline(
    store: KBStore,
    entity_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    order: str = ORDER_EFFECTIVE,
    types: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Order an entity's approved claims + relations along a time axis.

    Entries are ``{when, kind, id, summary, status}``, oldest first
    (most-recent-last). ``status`` is the claim's current :class:`ClaimStatus`
    (a superseded/archived claim still appears, flagged); relations have no
    status, so it is ``None``. When ``limit`` is set, the most recent ``limit``
    entries are kept, still in chronological order. Raises
    :class:`~vouch.storage.ArtifactNotFoundError` if the entity does not exist.
    """
    if order not in ORDERS:
        raise TimelineError(f"order must be one of {ORDERS}, got {order!r}")
    if limit is not None and limit < 0:
        raise TimelineError("limit must be >= 0")
    since = _as_utc(since)
    until = _as_utc(until)

    entity = store.get_entity(entity_id)  # raises ArtifactNotFoundError

    want = {t.strip() for t in types if t.strip()} if types else None
    decided = _decided_map(store) if order == ORDER_DECIDED else {}

    def when_for(artifact_id: str, created: datetime | None) -> datetime | None:
        eff = _as_utc(created)
        if order == ORDER_DECIDED:
            return decided.get(artifact_id) or eff
        return eff

    rows: list[dict[str, Any]] = []

    for c in store.list_claims():
        if entity_id not in c.entities:
            continue
        # type filter: a claim matches on its ClaimType, or the generic "claim".
        if want is not None and c.type.value not in want and "claim" not in want:
            continue
        rows.append(
            {
                "_when": when_for(c.id, c.created_at),
                "kind": "claim",
                "id": c.id,
                "summary": _claim_summary(c),
                "status": c.status.value,
            }
        )

    for r in store.list_relations():
        if entity_id not in (r.source, r.target):
            continue
        # type filter: a relation matches on its RelationType, or "relation".
        if want is not None and r.relation.value not in want and "relation" not in want:
            continue
        rows.append(
            {
                "_when": when_for(r.id, r.created_at),
                "kind": "relation",
                "id": r.id,
                "summary": _relation_summary(r),
                "status": None,
            }
        )

    # window filter on the chosen timestamp
    if since is not None:
        rows = [e for e in rows if e["_when"] is not None and e["_when"] >= since]
    if until is not None:
        rows = [e for e in rows if e["_when"] is not None and e["_when"] <= until]

    # oldest first; id tie-break for deterministic output on identical stamps.
    # entries with no recoverable timestamp sort to the front (epoch).
    _epoch = datetime(1970, 1, 1, tzinfo=UTC)
    rows.sort(key=lambda e: (e["_when"] or _epoch, e["id"]))

    total = len(rows)
    if limit is not None and limit < total:
        rows = rows[total - limit :]  # keep the most recent `limit`, still chronological

    entries = [
        {"when": _iso(e["_when"]), "kind": e["kind"], "id": e["id"],
         "summary": e["summary"], "status": e["status"]}
        for e in rows
    ]

    return {
        "entity": {"id": entity.id, "name": entity.name, "type": entity.type.value},
        "order": order,
        "count": len(entries),
        "total": total,
        "entries": entries,
    }
