"""Typed nodes and edges for the provenance DAG.

Edge orientation is uniform: ``A --kind--> B`` means *A is explained by / derives
from / depends on B*. So a claim points at the source it cites, the session it
was proposed in, the audit event that approved it, and the older claim it
supersedes. ``why`` therefore walks edges *outward* from a node; ``impact`` walks
them *inward* (who points at me). The two reverse kinds (``supersededBy`` /
``contradictedBy``) are query-time labels for inbound traversal — only the seven
canonical kinds in :data:`STORED_KINDS` are ever persisted, which keeps the
``prov_edges`` cache free of duplicate mirror rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class EdgeKind(StrEnum):
    CITES = "cites"  # claim -> source / evidence it cites
    DERIVED_FROM = "derivedFrom"  # evidence span -> source it quotes
    SUPERSEDES = "supersedes"  # newer claim -> older claim it replaces
    SUPERSEDED_BY = "supersededBy"  # reverse view of supersedes (query-time only)
    CONTRADICTS = "contradicts"  # claim -> claim it contradicts
    CONTRADICTED_BY = "contradictedBy"  # reverse view (query-time only)
    EMBEDS = "embeds"  # page -> claim it embeds as evidence
    PROPOSED_IN = "proposedIn"  # claim -> session it was proposed in
    APPROVED_BY = "approvedBy"  # claim -> audit event that approved it


class NodeKind(StrEnum):
    CLAIM = "claim"
    SOURCE = "source"
    EVIDENCE = "evidence"
    PAGE = "page"
    SESSION = "session"
    EVENT = "event"
    UNKNOWN = "unknown"


#: Edge kinds actually written to the ``prov_edges`` cache. The two ``*By``
#: mirrors are derived at query time by walking these inbound.
STORED_KINDS: frozenset[EdgeKind] = frozenset(
    {
        EdgeKind.CITES,
        EdgeKind.DERIVED_FROM,
        EdgeKind.SUPERSEDES,
        EdgeKind.CONTRADICTS,
        EdgeKind.EMBEDS,
        EdgeKind.PROPOSED_IN,
        EdgeKind.APPROVED_BY,
    }
)

#: "Why is this here" kinds, followed outward from a node by :func:`why`.
PROVENANCE_KINDS: frozenset[EdgeKind] = frozenset(
    {
        EdgeKind.CITES,
        EdgeKind.DERIVED_FROM,
        EdgeKind.SUPERSEDES,
        EdgeKind.CONTRADICTS,
        EdgeKind.PROPOSED_IN,
        EdgeKind.APPROVED_BY,
    }
)

#: Inbound edge kind -> the label used when it is reported as a dependency.
REVERSE_LABEL: dict[EdgeKind, EdgeKind] = {
    EdgeKind.SUPERSEDES: EdgeKind.SUPERSEDED_BY,
    EdgeKind.CONTRADICTS: EdgeKind.CONTRADICTED_BY,
}


@dataclass(frozen=True)
class Edge:
    """One typed, directed provenance edge.

    ``event_ts`` is the ISO-8601 timestamp that grounds the edge in the audit
    trail (the approval time, the claim's last update, etc.); ``session_id`` is
    the agent run the edge originated from, when known. Both are best-effort and
    carried through to the cache so cold queries can group/order without a
    rebuild.
    """

    src_id: str
    dst_id: str
    kind: EdgeKind
    event_ts: str = ""
    session_id: str | None = None

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.src_id, self.dst_id, self.kind.value)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "src": self.src_id,
            "dst": self.dst_id,
            "kind": self.kind.value,
            "event_ts": self.event_ts,
            "session_id": self.session_id,
        }


def sort_edges(edges: Iterable[Edge]) -> list[Edge]:
    """Deterministic edge ordering — the basis for cache/build equivalence."""
    return sorted(edges, key=lambda e: e.sort_key)
