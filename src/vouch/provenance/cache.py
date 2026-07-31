"""Persist and reload the provenance DAG via the ``prov_edges`` table.

The cache is a pure acceleration: :func:`rebuild_prov_edges` serialises exactly
what :func:`~.graph.build_graph` produces — edges into ``prov_edges``, node kind
/ status / label into ``prov_nodes`` — and :func:`load_graph` returns a graph
backed by the cache when it is fresh, transparently rebuilding it when the KB has
changed underneath. A freshness *stamp* (claim count + page count + audit event
count + pending proposal count) is stored alongside the rows so a cold query
knows whether the cache can be trusted without re-reading every file.
"""

from __future__ import annotations

from .. import audit, index_db
from ..storage import KBStore
from .graph import ProvGraph, build_graph
from .model import Edge, EdgeKind, NodeKind, NodeMeta

_STAMP_KEY = "prov_stamp"


def _pending_count(store: KBStore) -> int:
    """Pending proposals on disk, counted without parsing them."""
    return sum(1 for _ in (store.kb_dir / "proposed").glob("*.yaml"))


def prov_stamp(store: KBStore) -> str:
    """A cheap fingerprint of the inputs the graph is derived from.

    Counting beats hashing here: it is O(files) without parsing every claim,
    and any propose/approve/lifecycle action changes at least one of the four
    counts (the audit log is append-only, so its count is monotonic). The
    pending count is what a proposal filed and then rejected moves, and its
    presence in the stamp is also what invalidates every cache written before
    pending proposals were graph nodes.
    """
    claims = len(store.list_claims())
    pages = len(store.list_pages())
    events = audit.count_events(store.kb_dir)
    pending = _pending_count(store)
    return f"{claims}:{pages}:{events}:{pending}"


def rebuild_prov_edges(store: KBStore) -> int:
    """Rebuild ``prov_edges`` from scratch in one pass. Returns the edge count.

    This is what ``vouch provenance rebuild`` calls. It is the cold-path
    equivalent of the live graph: identical inputs yield an identical table.
    """
    graph = build_graph(store)
    stamp = prov_stamp(store)
    with index_db.open_db(store.kb_dir) as conn:
        index_db.clear_prov_edges(conn)
        for e in graph.edges:
            index_db.index_prov_edge(
                conn,
                src_id=e.src_id,
                dst_id=e.dst_id,
                kind=e.kind.value,
                event_ts=e.event_ts,
                session_id=e.session_id,
            )
        for node, meta in sorted(graph.meta().items()):
            index_db.index_prov_node(
                conn,
                id=node,
                kind=meta.kind.value,
                status=meta.status,
                label=meta.label,
            )
        index_db.set_meta(conn, _STAMP_KEY, stamp)
    return len(graph.edges)


def load_edges(store: KBStore) -> list[Edge]:
    """Load cached edges as :class:`Edge` objects (deterministically ordered)."""
    rows = index_db.read_prov_edges(store.kb_dir)
    return [
        Edge(
            src_id=src,
            dst_id=dst,
            kind=EdgeKind(kind),
            event_ts=ts or "",
            session_id=session,
        )
        for (src, dst, kind, ts, session) in rows
    ]


def load_meta(store: KBStore) -> dict[str, NodeMeta]:
    """Load cached node facts. An id whose kind is no longer known is dropped."""
    meta: dict[str, NodeMeta] = {}
    for node, kind, status, label in index_db.read_prov_nodes(store.kb_dir):
        try:
            parsed = NodeKind(kind)
        except ValueError:
            continue
        meta[node] = NodeMeta(parsed, status, label)
    return meta


def load_graph(store: KBStore, *, use_cache: bool = True) -> ProvGraph:
    """Return a provenance graph, using the cache when fresh.

    With ``use_cache=False`` (or a stale/empty cache) the graph is rebuilt from
    durable files and the cache is refreshed as a side effect, so the next call
    is hot. Correctness never depends on the cache: a rebuild is always exact.
    """
    if not use_cache:
        return build_graph(store)
    try:
        cached_stamp = index_db.get_meta(store.kb_dir, _STAMP_KEY)
    except Exception:
        cached_stamp = None
    if cached_stamp is None or cached_stamp != prov_stamp(store):
        rebuild_prov_edges(store)
    return ProvGraph(load_edges(store), load_meta(store))
