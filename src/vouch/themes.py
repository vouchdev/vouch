"""Cross-session pattern detection — recurring entity clusters.

Scans approved claims across completed sessions, finds entity co-occurrence
clusters, and optionally proposes "theme" synthesis pages through the
review gate. All scoring is deterministic (no LLM). The detector never
writes directly — it only reads or proposes.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import yaml

from .models import ClaimStatus, ProposalStatus
from .proposals import ProposalError, propose_page
from .storage import KBStore

logger = logging.getLogger(__name__)

# Statuses that disqualify a claim from theme support.
_EXCLUDED_STATUSES = frozenset({
    ClaimStatus.ARCHIVED,
    ClaimStatus.SUPERSEDED,
    ClaimStatus.REDACTED,
})


@dataclass
class ThemeCluster:
    """A detected entity co-occurrence cluster."""

    entities: list[str]
    claim_ids: list[str]
    session_ids: list[str]
    score: float
    session_count: int
    claim_count: int


@dataclass
class DetectResult:
    """Outcome of detect_themes."""

    clusters: list[ThemeCluster] = field(default_factory=list)
    config_used: dict[str, Any] = field(default_factory=dict)


_DEFAULT_MIN_SESSIONS = 2
_DEFAULT_MIN_CLAIMS = 3
_DEFAULT_TOP_K = 10


def _load_theme_config(store: KBStore) -> dict[str, Any]:
    """Read theme-detection config with defensive defaults.

    Mirrors the salience.reflex_cfg pattern: every value is type-checked
    and falls back to its default rather than crashing on malformed input.
    """
    try:
        raw = yaml.safe_load(store.config_path.read_text())
        cfg = raw if isinstance(raw, dict) else {}
    except Exception:
        cfg = {}
    themes_cfg = cfg.get("themes") if isinstance(cfg, dict) else None
    if not isinstance(themes_cfg, dict):
        themes_cfg = {}

    enabled = themes_cfg.get("enabled", True)
    enabled = bool(enabled) if isinstance(enabled, bool) else True

    ms = themes_cfg.get("min_sessions", _DEFAULT_MIN_SESSIONS)
    ms = ms if isinstance(ms, int) and ms > 0 else _DEFAULT_MIN_SESSIONS

    mc = themes_cfg.get("min_claims", _DEFAULT_MIN_CLAIMS)
    mc = mc if isinstance(mc, int) and mc > 0 else _DEFAULT_MIN_CLAIMS

    tk = themes_cfg.get("top_k", _DEFAULT_TOP_K)
    tk = tk if isinstance(tk, int) and tk > 0 else _DEFAULT_TOP_K

    return {
        "enabled": enabled,
        "min_sessions": ms,
        "min_claims": mc,
        "top_k": tk,
    }


def detect_themes(
    store: KBStore,
    *,
    min_sessions: int | None = None,
    min_claims: int | None = None,
    top_k: int | None = None,
) -> DetectResult:
    """Detect recurring entity clusters across sessions.

    Pure read-only operation. Returns ranked clusters without persisting
    anything. Excludes archived, superseded, redacted, and pending claims.
    """
    cfg = _load_theme_config(store)
    if not cfg["enabled"]:
        return DetectResult(clusters=[], config_used=cfg)

    ms = min_sessions if min_sessions is not None else cfg["min_sessions"]
    mc = min_claims if min_claims is not None else cfg["min_claims"]
    tk = top_k if top_k is not None else cfg["top_k"]

    # Collect approved claims that reference entities and belong to sessions.
    claims = store.list_claims()
    # Also exclude pending (working) — only look at review-gated claims.
    eligible = [
        c for c in claims
        if c.status not in _EXCLUDED_STATUSES
        and c.entities
        and c.approved_by is not None
    ]

    # Map each claim to its session(s) via decided proposals.
    claim_session: dict[str, str] = {}
    for prop in store.list_proposals(ProposalStatus.APPROVED):
        if prop.kind.value == "claim" and prop.session_id:
            claim_id = prop.payload.get("id", "")
            if claim_id:
                claim_session[claim_id] = prop.session_id

    # Build entity pair co-occurrence across sessions.
    # Key: frozenset of two entity ids → {session_id: [claim_ids]}
    pair_evidence: dict[frozenset[str], dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for claim in eligible:
        sid = claim_session.get(claim.id)
        if not sid:
            continue
        ents = sorted(set(claim.entities))
        for i, e1 in enumerate(ents):
            for e2 in ents[i + 1:]:
                pair_evidence[frozenset({e1, e2})][sid].append(claim.id)

    # Score each pair: session_count * log(1 + claim_count).
    raw_clusters: list[ThemeCluster] = []
    for pair, sessions_map in pair_evidence.items():
        session_count = len(sessions_map)
        if session_count < ms:
            continue
        all_claim_ids = sorted({
            cid for cids in sessions_map.values() for cid in cids
        })
        if len(all_claim_ids) < mc:
            continue
        score = session_count * math.log(1 + len(all_claim_ids))
        raw_clusters.append(ThemeCluster(
            entities=sorted(pair),
            claim_ids=all_claim_ids,
            session_ids=sorted(sessions_map.keys()),
            score=round(score, 4),
            session_count=session_count,
            claim_count=len(all_claim_ids),
        ))

    # Merge overlapping pairs into larger clusters.
    clusters = _merge_clusters(raw_clusters, min_sessions=ms, min_claims=mc)

    # Deduplicate against existing theme pages. Compare on the resolvable
    # entity subset (the set that propose_theme would actually store) so
    # dedup stays consistent even when some cluster entities don't resolve.
    existing_themes = _existing_theme_entity_sets(store)
    resolvable = _resolvable_entities(store)
    clusters = [
        c for c in clusters
        if frozenset(e for e in c.entities if e in resolvable)
        not in existing_themes
    ]

    # Rank by score descending, take top_k.
    clusters.sort(key=lambda c: c.score, reverse=True)
    clusters = clusters[:tk]

    return DetectResult(clusters=clusters, config_used={
        "min_sessions": ms, "min_claims": mc, "top_k": tk, "enabled": True,
    })


def _merge_clusters(
    pairs: list[ThemeCluster],
    *,
    min_sessions: int,
    min_claims: int,
) -> list[ThemeCluster]:
    """Merge entity pairs that share entities into larger clusters."""
    if not pairs:
        return []

    # Union-find over entities.
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

    for cluster in pairs:
        ents = cluster.entities
        for i in range(len(ents) - 1):
            union(ents[i], ents[i + 1])

    # Group pairs by their root entity.
    groups: dict[str, list[ThemeCluster]] = defaultdict(list)
    for cluster in pairs:
        root = find(cluster.entities[0])
        groups[root].append(cluster)

    merged: list[ThemeCluster] = []
    for group in groups.values():
        all_entities: set[str] = set()
        all_claims: set[str] = set()
        all_sessions: set[str] = set()
        for c in group:
            all_entities.update(c.entities)
            all_claims.update(c.claim_ids)
            all_sessions.update(c.session_ids)

        if len(all_sessions) < min_sessions or len(all_claims) < min_claims:
            continue

        score = len(all_sessions) * math.log(1 + len(all_claims))
        merged.append(ThemeCluster(
            entities=sorted(all_entities),
            claim_ids=sorted(all_claims),
            session_ids=sorted(all_sessions),
            score=round(score, 4),
            session_count=len(all_sessions),
            claim_count=len(all_claims),
        ))
    return merged


def _resolvable_entities(store: KBStore) -> set[str]:
    """Return the set of entity ids that exist in the store."""
    return {e.id for e in store.list_entities()}


def _existing_theme_entity_sets(store: KBStore) -> set[frozenset[str]]:
    """Return entity sets of existing theme pages and pending theme proposals."""
    result: set[frozenset[str]] = set()
    for page in store.list_pages():
        if page.type == "theme" and page.entities:
            result.add(frozenset(page.entities))
    for prop in store.list_proposals(ProposalStatus.PENDING):
        if (prop.kind.value == "page"
                and prop.payload.get("type") == "theme"
                and prop.payload.get("entities")):
            result.add(frozenset(prop.payload["entities"]))
    return result


def propose_theme(
    store: KBStore,
    cluster: ThemeCluster,
    *,
    proposed_by: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """File a theme synthesis page through the review gate.

    The page body is deterministic (no LLM). It lists the entities, the
    supporting claims, and the sessions that contribute to the cluster.
    """
    # Guard: must have entities and claims.
    if not cluster.entities:
        raise ProposalError("cluster has no entities")
    if not cluster.claim_ids:
        raise ProposalError("cluster has no supporting claims")

    # Verify all referenced claims still exist and are eligible.
    valid_claims: list[str] = []
    for cid in cluster.claim_ids:
        try:
            claim = store.get_claim(cid)
            if claim.status not in _EXCLUDED_STATUSES:
                valid_claims.append(cid)
        except Exception:
            pass
    if not valid_claims:
        raise ProposalError("no eligible claims remain in cluster")

    # Verify entities exist.
    valid_entities: list[str] = []
    for eid in cluster.entities:
        try:
            store.get_entity(eid)
            valid_entities.append(eid)
        except Exception:
            pass
    if not valid_entities:
        raise ProposalError("no valid entities in cluster")

    slug = "theme-" + "-".join(valid_entities[:4])
    title = f"theme: {', '.join(valid_entities)}"
    body = _build_theme_body(cluster, valid_claims, valid_entities)

    proposal = propose_page(
        store,
        title=title,
        body=body,
        page_type="theme",
        claim_ids=valid_claims,
        entity_ids=valid_entities,
        proposed_by=proposed_by,
        tags=["theme", "auto-detected"],
        slug_hint=slug,
        session_id=session_id,
        rationale=(
            f"recurring pattern across {cluster.session_count} sessions, "
            f"{cluster.claim_count} claims (score {cluster.score})"
        ),
    )
    return {
        "proposal_id": proposal.id,
        "theme_page_id": slug,
        "entities": valid_entities,
        "claim_count": len(valid_claims),
        "session_count": cluster.session_count,
        "score": cluster.score,
    }


def _build_theme_body(
    cluster: ThemeCluster,
    claim_ids: list[str],
    entity_ids: list[str],
) -> str:
    lines = [
        f"# theme: {', '.join(entity_ids)}",
        "",
        f"recurring pattern detected across {cluster.session_count} sessions "
        f"with {len(claim_ids)} supporting claims.",
        "",
        "## entities",
        "",
    ]
    for eid in entity_ids:
        lines.append(f"- `{eid}`")
    lines.extend([
        "",
        "## supporting claims",
        "",
    ])
    for cid in claim_ids:
        lines.append(f"- `{cid}`")
    lines.extend([
        "",
        "## sessions",
        "",
    ])
    for sid in cluster.session_ids:
        lines.append(f"- `{sid}`")
    lines.extend([
        "",
        f"**score:** {cluster.score}",
    ])
    return "\n".join(lines)
