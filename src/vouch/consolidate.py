"""Retroactive consolidation — batch-propose supersedes for near-duplicate claims.

Clusters near-duplicate *already-approved* claims by embedding cosine
similarity, picks a deterministic survivor per cluster, and emits
supersede/merge intents into the pending queue. The pass never mutates
durable claims directly — it only proposes through the review gate.

Distinct from `embeddings.dedup.scan_all` (read-only reporter) and from
`proposals.propose_claim` (ingest-time path). This module is the retroactive
cleanup pass that turns detected duplicates into actionable proposals.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import audit
from .models import ClaimStatus, ProposalKind, ProposalStatus
from .proposals import _file_proposal, propose_claim
from .storage import KBStore

logger = logging.getLogger(__name__)

# Statuses that exclude a claim from consolidation input. Already
# superseded, archived, contested, or redacted claims should not be
# re-proposed — the issue spec is explicit about this.
_EXCLUDED_STATUSES = frozenset({
    ClaimStatus.SUPERSEDED,
    ClaimStatus.ARCHIVED,
    ClaimStatus.CONTESTED,
    ClaimStatus.REDACTED,
})

_DEFAULT_THRESHOLD = 0.95
_DEFAULT_MODE = "supersede"
_DEFAULT_MAX_CLUSTERS = 50


@dataclass
class ClusterMember:
    """One claim within a consolidation cluster."""

    claim_id: str
    cosine: float  # similarity to the survivor


@dataclass
class ConsolidationCluster:
    """A group of near-duplicate claims with a nominated survivor."""

    survivor_id: str
    members: list[ClusterMember]
    cosine_min: float
    cosine_max: float


@dataclass
class ConsolidateResult:
    """Outcome of consolidate()."""

    clusters: list[ConsolidationCluster] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    config_used: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


def _load_consolidate_config(store: KBStore) -> dict[str, Any]:
    """Read consolidation config with defensive defaults.

    Mirrors the themes._load_theme_config pattern: every value is
    type-checked and falls back to its default on malformed input.
    """
    try:
        raw = yaml.safe_load(store.config_path.read_text())
        cfg = raw if isinstance(raw, dict) else {}
    except Exception:
        cfg = {}
    cons_cfg = cfg.get("consolidate") if isinstance(cfg, dict) else None
    if not isinstance(cons_cfg, dict):
        cons_cfg = {}

    threshold = cons_cfg.get("threshold", _DEFAULT_THRESHOLD)
    threshold = (
        threshold
        if isinstance(threshold, (int, float)) and 0.0 < threshold <= 1.0
        else _DEFAULT_THRESHOLD
    )

    mode = cons_cfg.get("mode", _DEFAULT_MODE)
    mode = mode if mode in ("supersede", "merge") else _DEFAULT_MODE

    mc = cons_cfg.get("max_clusters", _DEFAULT_MAX_CLUSTERS)
    mc = mc if isinstance(mc, int) and mc > 0 else _DEFAULT_MAX_CLUSTERS

    return {
        "threshold": float(threshold),
        "mode": mode,
        "max_clusters": mc,
    }


def _select_survivor(store: KBStore, claim_ids: list[str]) -> str:
    """Pick the survivor from a cluster: highest confidence, then most
    recent updated_at, then lexicographic id for determinism.
    """
    claims = []
    for cid in claim_ids:
        with contextlib.suppress(Exception):
            claims.append(store.get_claim(cid))
    if not claims:
        return claim_ids[0]
    claims.sort(
        key=lambda c: (-c.confidence, c.updated_at.isoformat(), c.id),
        reverse=False,
    )
    # Highest confidence first: negate so ascending sort picks it.
    # Among ties: most recent updated_at (reverse chrono → want latest
    # first, so sort descending on timestamp string).
    # Final tiebreak: smallest id (ascending).
    best = claims[0]
    for c in claims[1:]:
        if (
            c.confidence > best.confidence
            or (c.confidence == best.confidence and c.updated_at > best.updated_at)
            or (c.confidence == best.confidence and c.updated_at == best.updated_at
                and c.id < best.id)
        ):
            best = c
    return best.id


def _cluster_claims(
    kb_dir: Path,
    eligible_ids: set[str],
    threshold: float,
) -> list[list[tuple[str, str, float]]]:
    """Group eligible claim ids into clusters by cosine similarity.

    Returns a list of clusters, where each cluster is a list of
    (claim_id_a, claim_id_b, cosine) pairs. Uses union-find to merge
    pairs that share members.

    Reuses the same vector-comparison logic as dedup.scan_all — loads
    from embedding_index, computes pairwise cosine on same-kind vecs.
    """
    try:
        import numpy as np  # noqa: I001
        from . import index_db
    except ImportError:
        return []

    with index_db.open_db(kb_dir) as conn:
        rows = conn.execute(
            "SELECT kind, id, vec, dim FROM embedding_index WHERE kind = 'claim'"
        ).fetchall()

    if not rows:
        return []

    # Build id→vec map for eligible claims only.
    vecs: dict[str, Any] = {}
    for _kind, cid, blob, dim in rows:
        if cid in eligible_ids:
            vecs[cid] = np.frombuffer(blob, dtype=np.float32, count=dim).copy()

    if len(vecs) < 2:
        return []

    # Pairwise cosine (O(n²) like scan_all).
    pairs: list[tuple[str, str, float]] = []
    keys = sorted(vecs.keys())
    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1:]:
            if vecs[k1].shape != vecs[k2].shape:
                continue
            cos = float(vecs[k1] @ vecs[k2])
            if cos >= threshold:
                pairs.append((k1, k2, cos))

    if not pairs:
        return []

    # Union-find to group pairs into clusters.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b, _cos in pairs:
        union(a, b)

    # Group pairs by root.
    from collections import defaultdict
    groups: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for a, b, cos in pairs:
        root = find(a)
        groups[root].append((a, b, cos))

    return list(groups.values())


def consolidate(
    store: KBStore,
    *,
    threshold: float | None = None,
    mode: str | None = None,
    max_clusters: int | None = None,
    dry_run: bool = False,
    actor: str = "consolidate-agent",
    session_id: str | None = None,
) -> ConsolidateResult:
    """Cluster near-duplicate approved claims and propose supersede/merge intents.

    In supersede mode: for each non-survivor, proposes a relation that on
    approval calls lifecycle.supersede(old=member, new=survivor).

    In merge mode: proposes a single new claim per cluster that unions the
    evidence/entities/tags, then supersedes every member on approval.

    dry_run=True returns clusters and intents without writing anything.
    """
    cfg = _load_consolidate_config(store)
    th = threshold if threshold is not None else cfg["threshold"]
    md = mode if mode is not None else cfg["mode"]
    mc = max_clusters if max_clusters is not None else cfg["max_clusters"]

    config_used = {"threshold": th, "mode": md, "max_clusters": mc}

    # Collect approved, eligible claims.
    all_claims = store.list_claims()
    eligible = {
        c.id for c in all_claims
        if c.status not in _EXCLUDED_STATUSES
        and c.approved_by is not None
    }

    if not eligible:
        return ConsolidateResult(config_used=config_used, dry_run=dry_run)

    # Cluster by cosine.
    raw_groups = _cluster_claims(store.kb_dir, eligible, th)
    if not raw_groups:
        return ConsolidateResult(config_used=config_used, dry_run=dry_run)

    # Build ConsolidationCluster objects.
    clusters: list[ConsolidationCluster] = []
    for group_pairs in raw_groups:
        # Collect all claim ids in this group.
        all_ids: set[str] = set()
        for a, b, _cos in group_pairs:
            all_ids.add(a)
            all_ids.add(b)

        survivor = _select_survivor(store, sorted(all_ids))

        # Build members with their cosine to the survivor.
        member_cosines: dict[str, float] = {}
        for a, b, cos in group_pairs:
            if a != survivor and (a not in member_cosines or cos > member_cosines[a]):
                member_cosines[a] = cos
            if b != survivor and (b not in member_cosines or cos > member_cosines[b]):
                member_cosines[b] = cos

        members = [
            ClusterMember(claim_id=cid, cosine=round(cos, 4))
            for cid, cos in sorted(member_cosines.items())
            if cid != survivor
        ]
        if not members:
            continue

        cosines = [m.cosine for m in members]
        clusters.append(ConsolidationCluster(
            survivor_id=survivor,
            members=members,
            cosine_min=min(cosines),
            cosine_max=max(cosines),
        ))

    # Cap clusters.
    clusters = clusters[:mc]

    if dry_run:
        audit.log_event(
            store.kb_dir,
            event="consolidate.scan",
            actor=actor,
            dry_run=True,
            data={
                "threshold": th,
                "mode": md,
                "cluster_count": len(clusters),
            },
        )
        return ConsolidateResult(
            clusters=clusters,
            config_used=config_used,
            dry_run=True,
        )

    # Propose intents.
    proposals: list[dict[str, Any]] = []

    if md == "supersede":
        proposals = _propose_supersede(
            store, clusters, actor=actor, session_id=session_id,
        )
    elif md == "merge":
        proposals = _propose_merge(
            store, clusters, actor=actor, session_id=session_id,
        )

    audit.log_event(
        store.kb_dir,
        event="consolidate.propose",
        actor=actor,
        object_ids=[p["proposal_id"] for p in proposals],
        data={
            "threshold": th,
            "mode": md,
            "cluster_count": len(clusters),
            "proposal_count": len(proposals),
        },
    )

    return ConsolidateResult(
        clusters=clusters,
        proposals=proposals,
        config_used=config_used,
        dry_run=False,
    )


def _propose_supersede(
    store: KBStore,
    clusters: list[ConsolidationCluster],
    *,
    actor: str,
    session_id: str | None,
) -> list[dict[str, Any]]:
    """For each non-survivor, file a pending supersede-intent proposal.

    Uses ProposalKind.RELATION with relation="supersedes" so that on
    approval the approve path can invoke lifecycle.supersede.
    """
    results: list[dict[str, Any]] = []

    for cluster in clusters:
        for member in cluster.members:
            # Check if a pending supersede proposal already exists.
            if _supersede_already_proposed(
                store, survivor=cluster.survivor_id, member=member.claim_id,
            ):
                continue

            payload = {
                "id": f"{cluster.survivor_id}--supersedes--{member.claim_id}",
                "source": cluster.survivor_id,
                "relation": "supersedes",
                "target": member.claim_id,
                "confidence": member.cosine,
                "evidence": [],
                "_consolidate": True,
            }
            proposal = _file_proposal(
                store,
                kind=ProposalKind.RELATION,
                payload=payload,
                proposed_by=actor,
                session_id=session_id,
                rationale=(
                    f"consolidation: {member.claim_id} is near-duplicate of "
                    f"{cluster.survivor_id} (cosine={member.cosine})"
                ),
                dry_run=False,
            )
            results.append({
                "proposal_id": proposal.id,
                "survivor": cluster.survivor_id,
                "member": member.claim_id,
                "cosine": member.cosine,
                "mode": "supersede",
            })

    return results


def _propose_merge(
    store: KBStore,
    clusters: list[ConsolidationCluster],
    *,
    actor: str,
    session_id: str | None,
) -> list[dict[str, Any]]:
    """For each cluster, propose a single union claim that supersedes all members.

    The union claim merges evidence, entities, and tags from every member
    (including the survivor). On approval the claim is created and each
    member should be superseded via lifecycle.supersede.
    """
    results: list[dict[str, Any]] = []

    for cluster in clusters:
        all_ids = sorted(
            {cluster.survivor_id} | {m.claim_id for m in cluster.members}
        )

        # Check if a merge proposal already exists for this cluster.
        if _merge_already_proposed(store, all_ids):
            continue

        # Union evidence, entities, tags from all cluster members.
        evidence_set: set[str] = set()
        entity_set: set[str] = set()
        tag_set: set[str] = set()
        texts: list[str] = []
        best_confidence = 0.0

        for cid in all_ids:
            try:
                claim = store.get_claim(cid)
                evidence_set.update(claim.evidence)
                entity_set.update(claim.entities)
                tag_set.update(claim.tags)
                texts.append(claim.text)
                if claim.confidence > best_confidence:
                    best_confidence = claim.confidence
            except Exception:
                pass

        if not evidence_set or not texts:
            continue

        # Use survivor's text as the canonical text.
        try:
            survivor_claim = store.get_claim(cluster.survivor_id)
            canonical_text = survivor_claim.text
            claim_type = survivor_claim.type.value
        except Exception:
            canonical_text = texts[0]
            claim_type = "observation"

        slug = f"merged-{cluster.survivor_id}"

        result = propose_claim(
            store,
            text=canonical_text,
            evidence=sorted(evidence_set),
            proposed_by=actor,
            claim_type=claim_type,
            confidence=best_confidence,
            entities=sorted(entity_set) if entity_set else None,
            tags=sorted(tag_set | {"consolidation-merge"}) if tag_set else ["consolidation-merge"],
            rationale=(
                f"consolidation merge of {len(all_ids)} near-duplicate claims: "
                f"{', '.join(all_ids)}"
            ),
            slug_hint=slug,
            session_id=session_id,
        )
        results.append({
            "proposal_id": result.id,
            "merged_claim_ids": all_ids,
            "cosine_min": cluster.cosine_min,
            "cosine_max": cluster.cosine_max,
            "mode": "merge",
        })

    return results


def _supersede_already_proposed(
    store: KBStore, *, survivor: str, member: str,
) -> bool:
    """Check if a pending supersede relation already exists for this pair."""
    for prop in store.list_proposals(ProposalStatus.PENDING):
        if (
            prop.kind == ProposalKind.RELATION
            and prop.payload.get("source") == survivor
            and prop.payload.get("target") == member
            and prop.payload.get("relation") == "supersedes"
        ):
            return True
    return False


def _merge_already_proposed(
    store: KBStore, claim_ids: list[str],
) -> bool:
    """Check if a pending merge claim already exists for this set of ids."""
    target_set = set(claim_ids)
    for prop in store.list_proposals(ProposalStatus.PENDING):
        if prop.kind == ProposalKind.CLAIM:
            rationale = prop.rationale or ""
            if (
                "consolidation merge" in rationale
                and all(cid in rationale for cid in target_set)
            ):
                return True
    return False
