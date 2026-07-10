"""Read-only source-usage report: which registered sources back real claims,
and which back nothing.

vouch's core invariant runs claim -> source: every claim cites a source. This
viewport reads it from the other end — source -> claim — to answer questions
the existing surfaces don't. `vouch lint` / `health` flag *dangling* citations
(a claim pointing at a source that isn't there); `vouch stats` reports the
coverage *rate*. Neither surfaces the inverse: a source that is registered,
still on disk, and cited by nothing. Those orphans are dead provenance — safe
to prune, or a prompt to mine the material for the claim it should support.

A source counts as cited when a claim rests on it through either citation path
vouch allows (Claim.evidence holds "Source ids OR Evidence ids"): directly, by
naming the source id, or indirectly, by naming an Evidence span that points
into the source. Both are resolved here, mirroring how `stats.citation_summary`
treats `ref in sources_present or ref in evidence_present`. A source is an
*orphan* when no claim cites it by either path AND no page references it in
`page.sources`; a source that a page references but no claim cites is left out
of both the cited and orphan counts — it is neither, so it appears in neither
actionable list, only in `sources_total`.

Strictly a viewport: it composes `store.list_sources`, `store.list_claims`,
`store.list_pages` and `store.list_evidence`, writes nothing, logs no audit
event, and never touches a proposal — there is nothing here for the review
gate to gate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .storage import KBStore

DEFAULT_LIMIT = 20


@dataclass(frozen=True)
class SourceUsage:
    id: str
    title: str | None
    type: str
    locator: str
    created_at: str
    # Distinct claims that cite this source directly (by its id) or indirectly
    # (by an Evidence span that points into it). A claim doing both is counted
    # once.
    claim_citations: int
    # Pages listing the source id in `page.sources`.
    page_references: int
    # Evidence records whose `source_id` is this source, whether or not any
    # claim cites them — informational, not part of the orphan test.
    evidence_spans: int
    is_orphan: bool


@dataclass(frozen=True)
class SourceUsageReport:
    """Stable `to_dict()` schema — the `--format json` contract."""

    generated_at: str
    limit: int
    sources_total: int
    cited_total: int
    orphan_total: int
    most_cited: list[SourceUsage] = field(default_factory=list)
    orphans: list[SourceUsage] = field(default_factory=list)

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
) -> SourceUsageReport:
    """Compose the source-usage report. Read-only by construction."""
    now = _as_utc(now) or datetime.now(UTC)

    sources = store.list_sources()
    claims = store.list_claims()
    pages = store.list_pages()
    evidence = store.list_evidence()

    source_ids = {s.id for s in sources}
    evidence_to_source = {ev.id: ev.source_id for ev in evidence}

    spans_by_source: dict[str, int] = defaultdict(int)
    for ev in evidence:
        spans_by_source[ev.source_id] += 1

    # Distinct claims per source, resolving both citation paths. A ref is an
    # Evidence id (resolve to its source) or a Source id; a ref that matches
    # neither is a dangling citation `health.lint` already reports, so skip it
    # rather than inventing a phantom source row for it.
    citing_claims: dict[str, set[str]] = defaultdict(set)
    for c in claims:
        for ref in c.evidence:
            if ref in evidence_to_source:
                citing_claims[evidence_to_source[ref]].add(c.id)
            elif ref in source_ids:
                citing_claims[ref].add(c.id)

    page_refs: dict[str, int] = defaultdict(int)
    for p in pages:
        for sid in p.sources:
            page_refs[sid] += 1

    rows: list[SourceUsage] = []
    for s in sources:
        claim_count = len(citing_claims.get(s.id, ()))
        page_count = page_refs.get(s.id, 0)
        rows.append(
            SourceUsage(
                id=s.id,
                title=s.title,
                type=s.type.value,
                locator=s.locator,
                created_at=(_as_utc(s.created_at) or now).isoformat(timespec="seconds"),
                claim_citations=claim_count,
                page_references=page_count,
                evidence_spans=spans_by_source.get(s.id, 0),
                is_orphan=claim_count == 0 and page_count == 0,
            )
        )

    most_cited = sorted(
        (r for r in rows if r.claim_citations > 0),
        key=lambda r: (-r.claim_citations, -r.page_references, r.id),
    )
    orphans = sorted(
        (r for r in rows if r.is_orphan),
        key=lambda r: (r.created_at, r.id),
    )

    return SourceUsageReport(
        generated_at=now.isoformat(timespec="seconds"),
        limit=limit,
        sources_total=len(rows),
        cited_total=sum(1 for r in rows if r.claim_citations > 0),
        orphan_total=len(orphans),
        most_cited=most_cited[:limit],
        orphans=orphans[:limit],
    )


def _label(r: SourceUsage) -> str:
    return r.title or r.locator or r.id


def render_text(report: SourceUsageReport) -> str:
    lines = [
        f"source usage @ {report.generated_at}",
        f"sources: {report.sources_total}  cited: {report.cited_total}  "
        f"orphaned: {report.orphan_total}",
        "",
        f"most cited ({len(report.most_cited)})",
    ]
    for r in report.most_cited:
        via = f", {r.evidence_spans} span(s)" if r.evidence_spans else ""
        lines.append(
            f"  {r.claim_citations:>4} claim(s){via}  {r.id[:12]}  [{r.type}]  {_label(r)}"
        )
    if not report.most_cited:
        lines.append("  none")
    if report.cited_total > len(report.most_cited):
        lines.append(f"  ... and {report.cited_total - len(report.most_cited)} more cited")
    lines.append("")
    lines.append(f"orphaned sources ({report.orphan_total})")
    for r in report.orphans:
        lines.append(f"  {r.id[:12]}  [{r.type}]  {r.created_at[:10]}  {_label(r)}")
    if not report.orphans:
        lines.append("  none")
    if report.orphan_total > len(report.orphans):
        lines.append(f"  ... and {report.orphan_total - len(report.orphans)} more")
    return "\n".join(lines)


def render_markdown(report: SourceUsageReport) -> str:
    lines = [
        f"# source usage — {report.generated_at}",
        "",
        f"sources: {report.sources_total} · cited: {report.cited_total} · "
        f"orphaned: {report.orphan_total}",
        "",
        f"## most cited ({len(report.most_cited)})",
    ]
    lines += [
        f"- `{r.id[:12]}` [{r.type}] {_label(r)} — {r.claim_citations} claim(s)"
        + (f", {r.evidence_spans} span(s)" if r.evidence_spans else "")
        for r in report.most_cited
    ] or ["- none"]
    if report.cited_total > len(report.most_cited):
        lines.append(f"- ... and {report.cited_total - len(report.most_cited)} more cited")
    lines.append("")
    lines.append(f"## orphaned sources ({report.orphan_total})")
    lines += [
        f"- `{r.id[:12]}` [{r.type}] {_label(r)} — registered {r.created_at[:10]}"
        for r in report.orphans
    ] or ["- none"]
    if report.orphan_total > len(report.orphans):
        lines.append(f"- ... and {report.orphan_total - len(report.orphans)} more")
    return "\n".join(lines)
