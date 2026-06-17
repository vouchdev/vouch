"""Build the provenance DAG from durable artifacts and traverse it.

:func:`build_graph` is the authoritative, always-fresh construction — it reads
claims, evidence, pages, sessions, approved proposals and the audit log and
emits the canonical edge set. The :mod:`.cache` layer persists exactly this set
to ``prov_edges`` and is validated against it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .. import audit
from ..models import ProposalKind, ProposalStatus
from ..storage import ArtifactNotFoundError, KBStore
from .model import Edge, EdgeKind, NodeKind, sort_edges


class ProvGraph:
    """An in-memory typed DAG with outward/inward/undirected traversal."""

    def __init__(
        self, edges: Iterable[Edge], node_kinds: dict[str, NodeKind] | None = None
    ) -> None:
        self.edges: list[Edge] = sort_edges(edges)
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}
        for e in self.edges:
            self._out.setdefault(e.src_id, []).append(e)
            self._in.setdefault(e.dst_id, []).append(e)
        self._node_kinds: dict[str, NodeKind] = dict(node_kinds or {})

    # --- node introspection -------------------------------------------------

    def nodes(self) -> set[str]:
        return set(self._out) | set(self._in)

    def kind_of(self, node: str) -> NodeKind:
        """Best-effort node kind, inferred from incident edges when unknown.

        Inference keeps cache-loaded graphs (which carry no explicit kind map)
        as informative as freshly-built ones.
        """
        known = self._node_kinds.get(node)
        if known is not None:
            return known
        for e in self._out.get(node, []):
            if e.kind in (
                EdgeKind.CITES,
                EdgeKind.SUPERSEDES,
                EdgeKind.CONTRADICTS,
                EdgeKind.PROPOSED_IN,
                EdgeKind.APPROVED_BY,
            ):
                return NodeKind.CLAIM
            if e.kind is EdgeKind.DERIVED_FROM:
                return NodeKind.EVIDENCE
            if e.kind is EdgeKind.EMBEDS:
                return NodeKind.PAGE
        for e in self._in.get(node, []):
            if e.kind is EdgeKind.EMBEDS:
                return NodeKind.CLAIM
            if e.kind is EdgeKind.PROPOSED_IN:
                return NodeKind.SESSION
            if e.kind is EdgeKind.APPROVED_BY:
                return NodeKind.EVENT
            if e.kind is EdgeKind.DERIVED_FROM:
                return NodeKind.SOURCE
            if e.kind is EdgeKind.CITES:
                return NodeKind.SOURCE
        return NodeKind.UNKNOWN

    # --- edge access --------------------------------------------------------

    def out_edges(
        self, node: str, kinds: Iterable[EdgeKind] | None = None
    ) -> list[Edge]:
        edges = self._out.get(node, [])
        if kinds is None:
            return list(edges)
        allow = set(kinds)
        return [e for e in edges if e.kind in allow]

    def in_edges(
        self, node: str, kinds: Iterable[EdgeKind] | None = None
    ) -> list[Edge]:
        edges = self._in.get(node, [])
        if kinds is None:
            return list(edges)
        allow = set(kinds)
        return [e for e in edges if e.kind in allow]

    # --- traversal ----------------------------------------------------------

    def shortest_path(self, start: str, goal: str) -> list[Edge] | None:
        """Shortest path between two nodes over an *undirected* view.

        Provenance edges are directed, but "how are these two artifacts
        related?" is a connectivity question, so an edge is crossable either
        way. Returns the list of edges along the path (in start->goal order), or
        ``None`` if disconnected. ``start == goal`` yields an empty path.
        """
        if start == goal:
            return [] if start in self.nodes() else None
        # BFS; remember the edge we crossed to reach each node.
        prev: dict[str, tuple[str, Edge]] = {}
        seen = {start}
        q: deque[str] = deque([start])
        while q:
            node = q.popleft()
            neighbours: list[tuple[str, Edge]] = [
                (e.dst_id, e) for e in self._out.get(node, [])
            ] + [(e.src_id, e) for e in self._in.get(node, [])]
            for nxt, edge in neighbours:
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = (node, edge)
                if nxt == goal:
                    return _reconstruct(prev, start, goal)
                q.append(nxt)
        return None


def _reconstruct(
    prev: dict[str, tuple[str, Edge]], start: str, goal: str
) -> list[Edge]:
    chain: list[Edge] = []
    cur = goal
    while cur != start:
        parent, edge = prev[cur]
        chain.append(edge)
        cur = parent
    chain.reverse()
    return chain


def build_graph(store: KBStore) -> ProvGraph:
    """Reconstruct the full provenance graph from durable files.

    Deterministic: claims and pages are read in sorted order and the audit log
    in append order, so the emitted edge set is stable across runs — that is
    what makes the ``prov_edges`` cache verifiable against it.
    """
    edges: dict[tuple[str, str, str], Edge] = {}
    node_kinds: dict[str, NodeKind] = {}

    def add(
        src: str,
        dst: str,
        kind: EdgeKind,
        ts: str = "",
        session: str | None = None,
    ) -> None:
        key = (src, dst, kind.value)
        if key not in edges:
            edges[key] = Edge(src, dst, kind, ts, session)

    claims = store.list_claims()
    claim_ids = {c.id for c in claims}
    for c in claims:
        node_kinds[c.id] = NodeKind.CLAIM

    # claim -> proposing session, from approved claim proposals
    proposed_in: dict[str, str] = {}
    for pr in store.list_proposals(ProposalStatus.APPROVED):
        if pr.kind is ProposalKind.CLAIM and pr.session_id:
            cid = pr.payload.get("id")
            if isinstance(cid, str):
                proposed_in[cid] = pr.session_id

    # claim -> approval audit event, from the append-only log
    approve_event: dict[str, tuple[str, str]] = {}
    for ev in audit.read_events(store.kb_dir):
        if not ev.event.endswith(".approve"):
            continue
        ts = ev.created_at.isoformat()
        for oid in ev.object_ids:
            if oid in claim_ids:
                approve_event[oid] = (ev.id, ts)

    for c in claims:
        c_ts = c.updated_at.isoformat()
        sess = proposed_in.get(c.id)

        for ref in c.evidence:
            add(c.id, ref, EdgeKind.CITES, c_ts, sess)
            try:
                evd = store.get_evidence(ref)
            except ArtifactNotFoundError:
                node_kinds.setdefault(ref, NodeKind.SOURCE)
            else:
                node_kinds[ref] = NodeKind.EVIDENCE
                node_kinds[evd.source_id] = NodeKind.SOURCE
                add(
                    ref,
                    evd.source_id,
                    EdgeKind.DERIVED_FROM,
                    evd.created_at.isoformat(),
                )

        for old in c.supersedes:
            add(c.id, old, EdgeKind.SUPERSEDES, c_ts, sess)
        if c.superseded_by:
            # Mirror of some newer claim's `supersedes`; record canonically
            # (newer -> older) so the edge exists even if the newer claim file
            # has not yet been re-read.
            add(c.superseded_by, c.id, EdgeKind.SUPERSEDES, c_ts)

        for other in c.contradicts:
            add(c.id, other, EdgeKind.CONTRADICTS, c_ts, sess)

        if sess:
            node_kinds[sess] = NodeKind.SESSION
            add(c.id, sess, EdgeKind.PROPOSED_IN, c_ts, sess)

        if c.id in approve_event:
            eid, ts = approve_event[c.id]
            node_kinds[eid] = NodeKind.EVENT
            add(c.id, eid, EdgeKind.APPROVED_BY, ts, sess)

    for p in store.list_pages():
        node_kinds[p.id] = NodeKind.PAGE
        p_ts = p.updated_at.isoformat()
        for cid in p.claims:
            add(p.id, cid, EdgeKind.EMBEDS, p_ts)

    return ProvGraph(edges.values(), node_kinds)
