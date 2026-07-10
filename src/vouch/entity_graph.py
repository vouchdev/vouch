"""Read-only entity-connectivity report: which entities anchor the graph, and
which anchor nothing.

An entity exists to be connected — vouch's own model note says entities "anchor
relations and aggregate the claims that mention them." `health.status` counts
entities; `vouch neighbors` walks the edges around one node. Neither answers, in
one pass over the whole graph, which entities are load-bearing hubs and which
are dead: proposed once, then never mentioned by a claim, never joined by a
relation, never referenced by a page.

An entity's connectivity here is the count of everything that points at it:
claims that mention it (`claim.entities`), relations with it at either endpoint
(a self-loop counts once), and pages that reference it (`page.entities`). An
entity is an *orphan* when that count is zero — dead weight a curator can prune
or merge into a live one.

Strictly a viewport: it composes `store.list_entities`, `list_claims`,
`list_relations` and `list_pages`, writes nothing, logs no audit event, and
never touches a proposal.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .storage import KBStore

DEFAULT_LIMIT = 20


@dataclass(frozen=True)
class EntityConnectivity:
    id: str
    name: str
    type: str
    created_at: str
    # Distinct claims whose `entities` list names this entity.
    claim_mentions: int
    # Relations with this entity at either endpoint; a self-loop counts once.
    relations: int
    # Pages whose `entities` list names this entity.
    page_references: int
    # claim_mentions + relations + page_references.
    connections: int
    is_orphan: bool


@dataclass(frozen=True)
class EntityGraphReport:
    """Stable `to_dict()` schema — the `--format json` contract."""

    generated_at: str
    limit: int
    entities_total: int
    connected_total: int
    orphan_total: int
    most_connected: list[EntityConnectivity] = field(default_factory=list)
    orphans: list[EntityConnectivity] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def build(
    store: KBStore,
    *,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> EntityGraphReport:
    """Compose the entity-connectivity report. Read-only by construction."""
    now = _as_utc(now) or datetime.now(UTC)

    entities = store.list_entities()
    entity_ids = {e.id for e in entities}

    claim_mentions: dict[str, int] = defaultdict(int)
    for c in store.list_claims():
        # A claim can list the same entity twice; count the entity once per
        # claim by collapsing to a set first.
        for eid in set(c.entities):
            if eid in entity_ids:
                claim_mentions[eid] += 1

    relation_degree: dict[str, int] = defaultdict(int)
    for r in store.list_relations():
        # A self-loop (source == target) is one edge on the node, not two.
        for eid in {r.source, r.target}:
            if eid in entity_ids:
                relation_degree[eid] += 1

    page_references: dict[str, int] = defaultdict(int)
    for p in store.list_pages():
        for eid in set(p.entities):
            if eid in entity_ids:
                page_references[eid] += 1

    rows: list[EntityConnectivity] = []
    for e in entities:
        mentions = claim_mentions.get(e.id, 0)
        rels = relation_degree.get(e.id, 0)
        pages = page_references.get(e.id, 0)
        total = mentions + rels + pages
        rows.append(
            EntityConnectivity(
                id=e.id,
                name=e.name,
                type=e.type.value,
                created_at=(_as_utc(e.created_at) or now).isoformat(timespec="seconds"),
                claim_mentions=mentions,
                relations=rels,
                page_references=pages,
                connections=total,
                is_orphan=total == 0,
            )
        )

    most_connected = sorted(
        (r for r in rows if r.connections > 0),
        key=lambda r: (-r.connections, -r.relations, r.id),
    )
    orphans = sorted(
        (r for r in rows if r.is_orphan),
        key=lambda r: (r.created_at, r.id),
    )

    return EntityGraphReport(
        generated_at=now.isoformat(timespec="seconds"),
        limit=limit,
        entities_total=len(rows),
        connected_total=sum(1 for r in rows if r.connections > 0),
        orphan_total=len(orphans),
        most_connected=most_connected[:limit],
        orphans=orphans[:limit],
    )


def render_text(report: EntityGraphReport) -> str:
    lines = [
        f"entity graph @ {report.generated_at}",
        f"entities: {report.entities_total}  connected: {report.connected_total}  "
        f"orphaned: {report.orphan_total}",
        "",
        f"most connected ({len(report.most_connected)})",
    ]
    for r in report.most_connected:
        lines.append(
            f"  {r.connections:>4} conn  ({r.claim_mentions} claim / {r.relations} rel / "
            f"{r.page_references} page)  {r.id}  [{r.type}]  {r.name}"
        )
    if not report.most_connected:
        lines.append("  none")
    if report.connected_total > len(report.most_connected):
        lines.append(f"  ... and {report.connected_total - len(report.most_connected)} more")
    lines.append("")
    lines.append(f"orphaned entities ({report.orphan_total})")
    for r in report.orphans:
        lines.append(f"  {r.id}  [{r.type}]  {r.created_at[:10]}  {r.name}")
    if not report.orphans:
        lines.append("  none")
    if report.orphan_total > len(report.orphans):
        lines.append(f"  ... and {report.orphan_total - len(report.orphans)} more")
    return "\n".join(lines)


def render_markdown(report: EntityGraphReport) -> str:
    lines = [
        f"# entity graph — {report.generated_at}",
        "",
        f"entities: {report.entities_total} · connected: {report.connected_total} · "
        f"orphaned: {report.orphan_total}",
        "",
        f"## most connected ({len(report.most_connected)})",
    ]
    lines += [
        f"- `{r.id}` [{r.type}] {r.name} — {r.connections} conn "
        f"({r.claim_mentions} claim / {r.relations} rel / {r.page_references} page)"
        for r in report.most_connected
    ] or ["- none"]
    if report.connected_total > len(report.most_connected):
        lines.append(f"- ... and {report.connected_total - len(report.most_connected)} more")
    lines.append("")
    lines.append(f"## orphaned entities ({report.orphan_total})")
    lines += [
        f"- `{r.id}` [{r.type}] {r.name} — registered {r.created_at[:10]}"
        for r in report.orphans
    ] or ["- none"]
    if report.orphan_total > len(report.orphans):
        lines.append(f"- ... and {report.orphan_total - len(report.orphans)} more")
    return "\n".join(lines)
