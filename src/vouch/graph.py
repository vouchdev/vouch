"""Graph traversal — `kb.neighbors` / `vouch neighbors`.

Walks Relation edges plus structural links on claims, pages, and entities
(supersedes, contradicts, mentions, includes). Used by context expansion
to pull related knowledge into a ContextPack after the initial search hits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .models import ClaimStatus, Relation
from .storage import ArtifactNotFoundError, KBStore

_RETRACTED_CLAIM_STATUSES = frozenset({
    ClaimStatus.ARCHIVED,
    ClaimStatus.SUPERSEDED,
    ClaimStatus.REDACTED,
})

NodeKind = Literal["claim", "page", "entity", "source"]


@dataclass(frozen=True)
class _Edge:
    source: str
    target: str
    relation: str
    relation_id: str | None = None


def _node_kind(store: KBStore, node_id: str) -> NodeKind:
    if store._claim_path(node_id).exists():
        return "claim"
    if store._page_path(node_id).exists():
        return "page"
    if store._entity_path(node_id).exists():
        return "entity"
    if (store._source_dir(node_id) / "meta.yaml").exists():
        return "source"
    raise ArtifactNotFoundError(f"node {node_id}")


def _summary_for(store: KBStore, kind: str, node_id: str) -> str:
    try:
        if kind == "claim":
            return store.get_claim(node_id).text
        if kind == "page":
            p = store.get_page(node_id)
            return p.title or p.body[:200]
        if kind == "entity":
            e = store.get_entity(node_id)
            return e.name or (e.description or "")[:200]
        if kind == "source":
            s = store.get_source(node_id)
            return s.title or s.locator or node_id
    except ArtifactNotFoundError:
        pass
    return ""


def _claim_is_retrievable(store: KBStore, claim_id: str) -> bool:
    try:
        claim = store.get_claim(claim_id)
    except ArtifactNotFoundError:
        return False
    return claim.status not in _RETRACTED_CLAIM_STATUSES


def _relation_allowed(rel: Relation, rel_types: frozenset[str] | None) -> bool:
    if rel_types is None:
        return True
    return rel.relation.value in rel_types


def _structural_edges(store: KBStore, node_id: str, kind: NodeKind) -> list[_Edge]:
    edges: list[_Edge] = []
    if kind == "claim":
        claim = store.get_claim(node_id)
        for eid in claim.entities:
            if store._entity_path(eid).exists():
                edges.append(_Edge(node_id, eid, "mentions"))
        for cid in claim.supersedes:
            if store._claim_path(cid).exists():
                edges.append(_Edge(node_id, cid, "supersedes"))
        if claim.superseded_by and store._claim_path(claim.superseded_by).exists():
            edges.append(_Edge(claim.superseded_by, node_id, "supersedes"))
        for cid in claim.contradicts:
            if store._claim_path(cid).exists():
                edges.append(_Edge(node_id, cid, "contradicts"))
    elif kind == "page":
        page = store.get_page(node_id)
        for cid in page.claims:
            if store._claim_path(cid).exists():
                edges.append(_Edge(node_id, cid, "includes_claim"))
        for eid in page.entities:
            if store._entity_path(eid).exists():
                edges.append(_Edge(node_id, eid, "mentions"))
        for sid in page.sources:
            if (store._source_dir(sid) / "meta.yaml").exists():
                edges.append(_Edge(node_id, sid, "references"))
    elif kind == "entity":
        entity = store.get_entity(node_id)
        if entity.page and store._page_path(entity.page).exists():
            edges.append(_Edge(node_id, entity.page, "described_by"))
    return edges


def _edges_from_node(
    store: KBStore,
    node_id: str,
    *,
    rel_types: frozenset[str] | None,
) -> list[_Edge]:
    edges: list[_Edge] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(edge: _Edge) -> None:
        key = (edge.source, edge.target, edge.relation)
        if key in seen:
            return
        if rel_types is not None and edge.relation not in rel_types:
            return
        seen.add(key)
        edges.append(edge)

    for rel in store.relations_from(node_id):
        if _relation_allowed(rel, rel_types):
            _add(_Edge(rel.source, rel.target, rel.relation.value, rel.id))
    for rel in store.relations_to(node_id):
        if _relation_allowed(rel, rel_types):
            _add(_Edge(rel.source, rel.target, rel.relation.value, rel.id))

    try:
        kind = _node_kind(store, node_id)
    except ArtifactNotFoundError:
        return edges

    for edge in _structural_edges(store, node_id, kind):
        _add(edge)
    return edges


def _neighbor_ok(store: KBStore, node_id: str, kind: NodeKind) -> bool:
    if kind == "claim":
        return _claim_is_retrievable(store, node_id)
    return store._node_exists(node_id)


def find_neighbors(
    store: KBStore,
    node_id: str,
    *,
    depth: int = 1,
    rel_types: list[str] | None = None,
    max_nodes: int = 50,
) -> dict[str, Any]:
    """Return nodes and edges reachable within `depth` hops of `node_id`."""
    if depth < 1:
        raise ValueError("depth must be >= 1")
    if max_nodes < 1:
        raise ValueError("max_nodes must be >= 1")

    root_kind = _node_kind(store, node_id)
    rel_filter = frozenset(rel_types) if rel_types else None

    visited: set[str] = {node_id}
    nodes: list[dict[str, Any]] = []
    edges_out: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    frontier = [node_id]

    for dist in range(1, depth + 1):
        next_frontier: list[str] = []
        for current in frontier:
            for edge in _edges_from_node(store, current, rel_types=rel_filter):
                other = edge.target if edge.source == current else edge.source
                if other not in visited:
                    try:
                        kind = _node_kind(store, other)
                    except ArtifactNotFoundError:
                        continue
                    if not _neighbor_ok(store, other, kind):
                        continue
                    visited.add(other)
                    next_frontier.append(other)
                    nodes.append({
                        "id": other,
                        "kind": kind,
                        "distance": dist,
                        "via": current,
                        "relation": edge.relation,
                        "summary": _summary_for(store, kind, other),
                    })
                ekey = (edge.source, edge.target, edge.relation)
                if ekey not in seen_edges:
                    seen_edges.add(ekey)
                    edges_out.append({
                        "source": edge.source,
                        "target": edge.target,
                        "relation": edge.relation,
                        "relation_id": edge.relation_id,
                    })
                if len(nodes) >= max_nodes:
                    break
            if len(nodes) >= max_nodes:
                break
        if len(nodes) >= max_nodes:
            break
        frontier = next_frontier

    return {
        "node_id": node_id,
        "kind": root_kind,
        "depth": depth,
        "nodes": nodes,
        "edges": edges_out,
    }


def graph_neighbors_for_seeds(
    store: KBStore,
    seed_ids: list[str],
    *,
    depth: int = 1,
    rel_types: list[str] | None = None,
    max_nodes: int = 20,
) -> list[dict[str, Any]]:
    """Collect unique neighbor nodes for several seed ids (context expansion)."""
    seen: set[str] = set(seed_ids)
    out: list[dict[str, Any]] = []
    for seed in seed_ids:
        try:
            result = find_neighbors(
                store, seed, depth=depth, rel_types=rel_types, max_nodes=max_nodes,
            )
        except ArtifactNotFoundError:
            continue
        for node in result["nodes"]:
            nid = node["id"]
            if nid in seen:
                continue
            seen.add(nid)
            out.append(node)
            if len(out) >= max_nodes:
                return out
    return out
