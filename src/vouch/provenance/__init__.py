"""Provenance DAG for the vouch review gate.

vouch already records everything needed to explain why a claim exists — the
session that proposed it, the source it cites, the supersedes chain it sits on,
the contradiction that demoted it, the page that embeds it as evidence — but it
is scattered across ``audit.log.jsonl``, ``relations/``, ``evidence/``,
``sessions/`` and the claim file itself. This package reconstructs a single
typed directed graph from those artifacts and answers three operational
questions:

* ``why``    — backward: what does this artifact derive from? (cites, session,
  supersedes chain, contradictions, approval event)
* ``impact`` — forward: what depends on this artifact, and what breaks if I
  archive / contradict / supersede it?
* ``trace``  — the shortest typed-edge path between two artifacts.

The graph is *derived state*. Nothing here is a source of truth: every edge is
rebuilt from durable files, and the persistent ``prov_edges`` table in
``state.db`` is a disposable cache that ``vouch provenance rebuild``
reconstructs byte-for-byte. All mutations still flow through the existing
proposal + lifecycle code paths.
"""

from __future__ import annotations

from .cache import load_graph, prov_stamp, rebuild_prov_edges
from .graph import ProvGraph, build_graph
from .model import Edge, EdgeKind, NodeKind
from .query import (
    LifecycleOp,
    graph_export,
    impact,
    render_impact,
    render_trace,
    render_why,
    trace,
    why,
)

__all__ = [
    "Edge",
    "EdgeKind",
    "LifecycleOp",
    "NodeKind",
    "ProvGraph",
    "build_graph",
    "graph_export",
    "impact",
    "load_graph",
    "prov_stamp",
    "rebuild_prov_edges",
    "render_impact",
    "render_trace",
    "render_why",
    "trace",
    "why",
]
