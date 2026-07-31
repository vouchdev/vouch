"""Build the provenance DAG from durable artifacts and traverse it.

:func:`build_graph` is the authoritative, always-fresh construction — it reads
claims, evidence, pages, sessions, approved proposals and the audit log and
emits the canonical edge set. The :mod:`.cache` layer persists exactly this set
to ``prov_edges`` and is validated against it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .. import audit
from ..models import PageStatus, Proposal, ProposalKind, ProposalStatus
from ..storage import ArtifactNotFoundError, KBStore
from .model import Edge, EdgeKind, NodeKind, NodeMeta, sort_edges


class ProvGraph:
    """An in-memory typed DAG with outward/inward/undirected traversal."""

    def __init__(
        self, edges: Iterable[Edge], node_meta: Mapping[str, NodeMeta] | None = None
    ) -> None:
        self.edges: list[Edge] = sort_edges(edges)
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}
        for e in self.edges:
            self._out.setdefault(e.src_id, []).append(e)
            self._in.setdefault(e.dst_id, []).append(e)
        self._meta: dict[str, NodeMeta] = dict(node_meta or {})

    # --- node introspection -------------------------------------------------

    def nodes(self) -> set[str]:
        return set(self._out) | set(self._in)

    def meta(self) -> dict[str, NodeMeta]:
        """Every node the build recorded, including any with no edges."""
        return dict(self._meta)

    def status_of(self, node: str) -> str:
        """The node's review status, or ``""`` for nodes that have none."""
        known = self._meta.get(node)
        return known.status if known is not None else ""

    def label_of(self, node: str) -> str:
        """The node's own words, falling back to its id."""
        known = self._meta.get(node)
        return known.label if known is not None and known.label else node

    def kind_of(self, node: str) -> NodeKind:
        """Best-effort node kind, inferred from incident edges when unknown.

        Inference keeps graphs built from a bare edge list (an older cache, a
        hand-assembled one in a test) as informative as freshly-built ones.
        """
        known = self._meta.get(node)
        if known is not None:
            return known.kind
        for e in self._out.get(node, []):
            if e.kind is EdgeKind.TARGETS:
                return NodeKind.PROPOSAL
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


#: The two accumulators `build_graph` hands to its per-artifact helpers.
_AddEdge = Callable[[str, str, EdgeKind, str, str | None], None]
_NoteNode = Callable[[str, NodeKind, str, str], None]


def _proposal_label(payload: Mapping[str, Any]) -> str:
    """A pending proposal's own words — the same fallback ``vouch pending`` uses."""
    for key in ("text", "title", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _payload_refs(payload: Mapping[str, Any], key: str) -> list[str]:
    """String ids under ``key``, tolerating a payload that predates the field."""
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [ref for ref in value if isinstance(ref, str)]


def _add_pending(pr: Proposal, add: _AddEdge, note: _NoteNode) -> None:
    """Wire one pending proposal into the graph.

    The node is keyed on the *proposal* id rather than on the artifact id the
    payload would create: until approval that artifact does not exist, and a
    proposal's prospective id may already be taken by a claim on disk — the
    collision `approve` exists to catch. Edges reuse the durable kinds, so a
    pending claim hangs off the sources it cites exactly as an approved one
    does, and the only thing separating them in a renderer is the status.
    """
    payload = pr.payload
    ts = pr.proposed_at.isoformat()
    note(pr.id, NodeKind.PROPOSAL, pr.status.value, _proposal_label(payload))
    if pr.session_id:
        note(pr.session_id, NodeKind.SESSION, "", "")
        add(pr.id, pr.session_id, EdgeKind.PROPOSED_IN, ts, pr.session_id)
    if pr.kind is ProposalKind.DELETE:
        # For a delete the payload id names an artifact that already exists —
        # the one edge in the graph that points at something on its way out.
        target = payload.get("id")
        if isinstance(target, str):
            add(pr.id, target, EdgeKind.TARGETS, ts, pr.session_id)
        return
    for ref in _payload_refs(payload, "evidence"):
        add(pr.id, ref, EdgeKind.CITES, ts, pr.session_id)
    for cid in _payload_refs(payload, "claims"):
        add(pr.id, cid, EdgeKind.EMBEDS, ts, pr.session_id)


def build_graph(store: KBStore) -> ProvGraph:
    """Reconstruct the full provenance graph from files on disk.

    Deterministic: claims and pages are read in sorted order and the audit log
    in append order, so the emitted edge set is stable across runs — that is
    what makes the ``prov_edges`` cache verifiable against it.

    Pending proposals are nodes too. They are the only part of the graph that
    has not been through the gate, which is exactly why a reviewer needs to see
    them: the frontier where the KB is about to change is not visible from the
    durable artifacts alone.
    """
    edges: dict[tuple[str, str, str], Edge] = {}
    nodes: dict[str, NodeMeta] = {}

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

    def note(node: str, kind: NodeKind, status: str = "", label: str = "") -> None:
        nodes[node] = NodeMeta(kind, status, label)

    claims = store.list_claims()
    claim_ids = {c.id for c in claims}
    for c in claims:
        note(c.id, NodeKind.CLAIM, c.status.value, c.text)

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
                nodes.setdefault(ref, NodeMeta(NodeKind.SOURCE))
            else:
                note(ref, NodeKind.EVIDENCE)
                note(evd.source_id, NodeKind.SOURCE)
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
            note(sess, NodeKind.SESSION)
            add(c.id, sess, EdgeKind.PROPOSED_IN, c_ts, sess)

        if c.id in approve_event:
            eid, ts = approve_event[c.id]
            note(eid, NodeKind.EVENT)
            add(c.id, eid, EdgeKind.APPROVED_BY, ts, sess)

    for p in store.list_pages():
        # Archived pages are intentional retirements. Leaving them in the
        # graph undoes archive for anything that renders it, and disagrees
        # with recall / digest / search / neighbors, which all read the same
        # live set.
        if p.status is PageStatus.ARCHIVED:
            continue
        note(p.id, NodeKind.PAGE, p.status.value, p.title)
        p_ts = p.updated_at.isoformat()
        for cid in p.claims:
            add(p.id, cid, EdgeKind.EMBEDS, p_ts)

    for pr in store.list_proposals(ProposalStatus.PENDING):
        _add_pending(pr, add, note)

    return ProvGraph(edges.values(), nodes)
