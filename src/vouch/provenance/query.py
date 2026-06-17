"""The three operational queries — ``why``, ``impact``, ``trace`` — plus graph
export. Each returns a stable JSON-able ``dict``/``str`` so the same shape backs
the CLI, the ``kb.*`` RPC methods and downstream tooling. Human rendering lives
here too (bare prose + indentation, no colour) so output diffs cleanly into a
``gh pr comment`` or a session log.
"""

from __future__ import annotations

from enum import StrEnum

from ..models import PageStatus
from ..storage import ArtifactNotFoundError, KBStore
from .cache import load_graph
from .graph import ProvGraph
from .model import PROVENANCE_KINDS, REVERSE_LABEL, EdgeKind, NodeKind

SCHEMA_VERSION = 1


class LifecycleOp(StrEnum):
    """Lifecycle operations whose blast radius :func:`impact` can dry-run."""

    ARCHIVE = "archive"
    CONTRADICT = "contradict"
    SUPERSEDE = "supersede"


def _ensure_known(store: KBStore, graph: ProvGraph, node_id: str) -> None:
    """Raise a clean not-found instead of silently returning an empty result."""
    if node_id in graph.nodes():
        return
    # Not an edge endpoint anywhere — defer to the store's own error for the
    # overwhelmingly common case (a mistyped claim id).
    store.get_claim(node_id)


# --- why ------------------------------------------------------------------


def _why_node(
    graph: ProvGraph, node: str, depth: int, path: frozenset[str]
) -> list[dict[str, object]]:
    if depth <= 0:
        return []
    out: list[dict[str, object]] = []
    edges = sorted(
        graph.out_edges(node, PROVENANCE_KINDS),
        key=lambda e: (e.kind.value, e.dst_id),
    )
    for e in edges:
        cycle = e.dst_id in path
        entry: dict[str, object] = {
            "kind": e.kind.value,
            "target": e.dst_id,
            "target_kind": graph.kind_of(e.dst_id).value,
            "event_ts": e.event_ts,
            "session_id": e.session_id,
            "cycle": cycle,
            "children": (
                []
                if cycle
                else _why_node(graph, e.dst_id, depth - 1, path | {e.dst_id})
            ),
        }
        out.append(entry)
    return out


def why(
    store: KBStore, *, claim_id: str, depth: int = 3, use_cache: bool = True
) -> dict[str, object]:
    """Backward provenance: what explains this artifact's existence.

    Walks edges outward (cites, derived-from, supersedes, contradicts,
    proposed-in, approved-by) up to ``depth`` hops, grouped by kind, each leaf
    carrying its audit timestamp and originating session.
    """
    if depth < 0:
        raise ValueError("depth must be >= 0")
    graph = load_graph(store, use_cache=use_cache)
    _ensure_known(store, graph, claim_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "root": claim_id,
        "node_kind": graph.kind_of(claim_id).value,
        "depth": depth,
        "provenance": _why_node(graph, claim_id, depth, frozenset({claim_id})),
    }


# --- impact ---------------------------------------------------------------


def _impact_node(
    graph: ProvGraph, node: str, depth: int, path: frozenset[str]
) -> list[dict[str, object]]:
    if depth <= 0:
        return []
    out: list[dict[str, object]] = []
    edges = sorted(
        graph.in_edges(node),
        key=lambda e: (e.kind.value, e.src_id),
    )
    for e in edges:
        cycle = e.src_id in path
        label = REVERSE_LABEL.get(e.kind, e.kind)
        entry: dict[str, object] = {
            "kind": label.value,
            "source": e.src_id,
            "source_kind": graph.kind_of(e.src_id).value,
            "event_ts": e.event_ts,
            "session_id": e.session_id,
            "cycle": cycle,
            "dependents": (
                []
                if cycle
                else _impact_node(graph, e.src_id, depth - 1, path | {e.src_id})
            ),
        }
        out.append(entry)
    return out


def _breakage(
    store: KBStore, graph: ProvGraph, node: str, op: LifecycleOp
) -> list[dict[str, object]]:
    """Artifacts that would carry a stale reference after ``op`` on ``node``.

    For every destructive op the load-bearing breakage is the set of *active*
    pages that embed the claim: archiving/contradicting/superseding it leaves
    those live pages pointing at evidence that no longer holds. Draft and
    already-archived pages are not counted — they are not in front of a reader.
    """
    broken: list[dict[str, object]] = []
    for e in graph.in_edges(node, [EdgeKind.EMBEDS]):
        try:
            page = store.get_page(e.src_id)
        except ArtifactNotFoundError:
            continue
        if page.status is PageStatus.ACTIVE:
            broken.append(
                {
                    "id": page.id,
                    "kind": NodeKind.PAGE.value,
                    "status": page.status.value,
                    "via": EdgeKind.EMBEDS.value,
                    "title": page.title,
                }
            )
    broken.sort(key=lambda b: str(b["id"]))
    return broken


def impact(
    store: KBStore,
    *,
    claim_id: str,
    depth: int = 1,
    op: LifecycleOp | str | None = None,
    use_cache: bool = True,
) -> dict[str, object]:
    """Forward impact: what depends on this artifact, and what breaks.

    ``op`` (``archive`` / ``contradict`` / ``supersede``) dry-runs the lifecycle
    operation against the in-memory graph and reports the breakage list without
    writing anything. ``blocking`` is true iff that list is non-empty.
    """
    if depth < 0:
        raise ValueError("depth must be >= 0")
    parsed_op = LifecycleOp(op) if op is not None else None
    graph = load_graph(store, use_cache=use_cache)
    _ensure_known(store, graph, claim_id)
    breakage = (
        _breakage(store, graph, claim_id, parsed_op) if parsed_op is not None else []
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "root": claim_id,
        "node_kind": graph.kind_of(claim_id).value,
        "depth": depth,
        "op": parsed_op.value if parsed_op is not None else None,
        "dependents": _impact_node(graph, claim_id, depth, frozenset({claim_id})),
        "breakage": breakage,
        "blocking": bool(breakage),
    }


# --- trace ----------------------------------------------------------------


def _path_nodes(start: str, edges: list) -> list[str]:  # type: ignore[type-arg]
    nodes = [start]
    cur = start
    for e in edges:
        nxt = e.dst_id if e.src_id == cur else e.src_id
        nodes.append(nxt)
        cur = nxt
    return nodes


def trace(
    store: KBStore, *, from_id: str, to_id: str, use_cache: bool = True
) -> dict[str, object]:
    """Shortest typed-edge path between two artifacts (edges crossable either
    way). ``found`` is false with an empty path when they are disconnected."""
    graph = load_graph(store, use_cache=use_cache)
    chain = graph.shortest_path(from_id, to_id)
    if chain is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "from": from_id,
            "to": to_id,
            "found": False,
            "length": 0,
            "nodes": [],
            "path": [],
        }
    nodes = _path_nodes(from_id, chain)
    steps: list[dict[str, object]] = []
    cur = from_id
    for e in chain:
        nxt = e.dst_id if e.src_id == cur else e.src_id
        steps.append(
            {
                "from": cur,
                "to": nxt,
                "kind": e.kind.value,
                "reversed": e.src_id != cur,
            }
        )
        cur = nxt
    return {
        "schema_version": SCHEMA_VERSION,
        "from": from_id,
        "to": to_id,
        "found": True,
        "length": len(chain),
        "nodes": nodes,
        "path": steps,
    }


# --- graph export ---------------------------------------------------------


def _session_subgraph_edges(graph: ProvGraph, session_id: str) -> list:  # type: ignore[type-arg]
    claim_set = {
        e.src_id
        for e in graph.edges
        if e.kind is EdgeKind.PROPOSED_IN and e.dst_id == session_id
    }
    return [
        e
        for e in graph.edges
        if e.src_id in claim_set
        or e.dst_id in claim_set
        or e.src_id == session_id
        or e.dst_id == session_id
    ]


def graph_export(
    store: KBStore,
    *,
    session: str | None = None,
    fmt: str = "dot",
    use_cache: bool = True,
) -> str:
    """Render the DAG (or one session's subgraph) as Graphviz ``dot`` or
    ``mermaid`` flowchart text."""
    if fmt not in ("dot", "mermaid"):
        raise ValueError(f"unknown graph format: {fmt!r} (use 'dot' or 'mermaid')")
    graph = load_graph(store, use_cache=use_cache)
    edges = (
        _session_subgraph_edges(graph, session) if session is not None else graph.edges
    )
    nodes: list[str] = []
    seen: set[str] = set()
    for e in edges:
        for n in (e.src_id, e.dst_id):
            if n not in seen:
                seen.add(n)
                nodes.append(n)
    nodes.sort()
    if fmt == "dot":
        return _to_dot(graph, nodes, edges)
    return _to_mermaid(graph, nodes, edges)


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _to_dot(graph: ProvGraph, nodes: list[str], edges: list) -> str:  # type: ignore[type-arg]
    lines = ["digraph provenance {", "  rankdir=LR;", '  node [shape=box];']
    for n in nodes:
        label = f"{n}\\n({graph.kind_of(n).value})"
        lines.append(f'  "{_dot_escape(n)}" [label="{_dot_escape(label)}"];')
    for e in edges:
        lines.append(
            f'  "{_dot_escape(e.src_id)}" -> "{_dot_escape(e.dst_id)}" '
            f'[label="{_dot_escape(e.kind.value)}"];'
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _to_mermaid(graph: ProvGraph, nodes: list[str], edges: list) -> str:  # type: ignore[type-arg]
    alias = {n: f"n{i}" for i, n in enumerate(nodes)}
    lines = ["flowchart LR"]
    for n in nodes:
        label = f"{n} ({graph.kind_of(n).value})".replace('"', "'")
        lines.append(f'  {alias[n]}["{label}"]')
    for e in edges:
        lines.append(
            f"  {alias[e.src_id]} -->|{e.kind.value}| {alias[e.dst_id]}"
        )
    return "\n".join(lines) + "\n"


# --- human rendering ------------------------------------------------------


def _render_tree(
    entries: list[dict[str, object]],
    indent: int,
    target_key: str,
    child_key: str,
) -> list[str]:
    lines: list[str] = []
    pad = "  " * indent
    for entry in entries:
        target = entry[target_key]
        kind = entry["kind"]
        kind_label = str(entry.get("source_kind") or entry.get("target_kind") or "")
        ts = entry.get("event_ts") or ""
        suffix = f"  [{ts}]" if ts else ""
        if entry.get("cycle"):
            suffix += "  (cycle)"
        lines.append(f"{pad}{kind} -> {target} ({kind_label}){suffix}")
        children = entry.get(child_key) or []
        if isinstance(children, list) and children:
            lines.extend(
                _render_tree(children, indent + 1, target_key, child_key)
            )
    return lines


def render_why(result: dict[str, object]) -> str:
    root = result["root"]
    node_kind = result["node_kind"]
    lines = [f"why {root} ({node_kind})"]
    prov = result.get("provenance") or []
    if not isinstance(prov, list) or not prov:
        lines.append("  (no recorded provenance)")
        return "\n".join(lines)
    lines.extend(_render_tree(prov, 1, "target", "children"))
    return "\n".join(lines)


def render_impact(result: dict[str, object]) -> str:
    root = result["root"]
    node_kind = result["node_kind"]
    lines = [f"impact {root} ({node_kind})"]
    deps = result.get("dependents") or []
    if not isinstance(deps, list) or not deps:
        lines.append("  (nothing depends on this)")
    else:
        lines.extend(_render_tree(deps, 1, "source", "dependents"))
    op = result.get("op")
    if op:
        breakage = result.get("breakage") or []
        if isinstance(breakage, list) and breakage:
            lines.append(f"would break on {op} ({len(breakage)}):")
            for b in breakage:
                lines.append(f"  {b['id']} ({b['kind']}, {b['status']})")
        else:
            lines.append(f"no breakage on {op}")
    return "\n".join(lines)


def render_trace(result: dict[str, object]) -> str:
    if not result["found"]:
        return f"no path: {result['from']} -> {result['to']}"
    steps = result.get("path") or []
    lines = [f"{result['from']} -> {result['to']} ({result['length']} hops)"]
    if isinstance(steps, list):
        for s in steps:
            arrow = "<-" if s.get("reversed") else "->"
            lines.append(f"  {s['from']} {arrow}[{s['kind']}] {s['to']}")
    return "\n".join(lines)
