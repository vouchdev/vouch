"""Thin-page enrichment — propose expansions for stub/sparse pages.

A page can land as a stub: a title, a one-line body, and few or no
`claims`/`sources` citations. This module finds those thin pages and drafts
an enriched revision synthesized *strictly* from related approved material
already in the KB — no external fetch, no invented sentences.

The heavy lifting is delegated to `synthesize.synthesize` (issue #222),
which already answers a query from approved, non-retracted claims with one
cited clause per claim. Reusing it here (rather than re-deriving clauses
from `context.build_context_pack` ourselves) is what keeps the "every
sentence traces to an approved claim" invariant identical between the two
features.

Like `compile.compile_kb`, this only ever proposes: the enriched page is
filed via `proposals.propose_page` with `slug_hint` set to the existing page
id, and lands as a PENDING proposal. Nothing is written durably without a
human `kb.approve`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from . import audit as audit_mod
from .models import Page
from .proposals import ProposalError, propose_page
from .storage import ArtifactNotFoundError, KBStore
from .synthesize import synthesize

DEFAULT_MIN_BODY_CHARS = 200
DEFAULT_MIN_CITATIONS = 2

# The proposer identity for enrichment drafts. Deliberately not the human
# triggering the run: the default review gate refuses self-approval, so the
# reviewer who approves an enrichment proposal must be a different actor
# than the proposer (same reasoning as compile.py's COMPILE_ACTOR).
ENRICH_ACTOR = "page-enricher"

# How wide a net synthesize() casts when gathering candidate claims, and how
# long the appended, cited addition to the body is allowed to grow. These
# are enrichment-pass internals, not part of the public surface (only the
# thin-page thresholds are configurable per the issue).
_SYNTHESIZE_DEPTH = 12
_SYNTHESIZE_MAX_CHARS = 2000


@dataclass(frozen=True)
class EnrichmentConfig:
    min_body_chars: int = DEFAULT_MIN_BODY_CHARS
    min_citations: int = DEFAULT_MIN_CITATIONS


def _coerce_int(value: Any, default: int) -> int:
    # A config typo (min_body_chars: two-hundred) must degrade to the
    # default, not take down every caller.
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def load_config(store: KBStore) -> EnrichmentConfig:
    """Read `enrichment:` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return EnrichmentConfig()
    if not isinstance(loaded, dict):
        return EnrichmentConfig()
    raw = loaded.get("enrichment")
    if not isinstance(raw, dict):
        return EnrichmentConfig()
    return EnrichmentConfig(
        min_body_chars=_coerce_int(
            raw.get("min_body_chars", DEFAULT_MIN_BODY_CHARS), DEFAULT_MIN_BODY_CHARS,
        ),
        min_citations=_coerce_int(
            raw.get("min_citations", DEFAULT_MIN_CITATIONS), DEFAULT_MIN_CITATIONS,
        ),
    )


def _resolve_thresholds(
    cfg: EnrichmentConfig,
    *,
    min_body_chars: int | None,
    min_citations: int | None,
) -> EnrichmentConfig:
    return EnrichmentConfig(
        min_body_chars=cfg.min_body_chars if min_body_chars is None else min_body_chars,
        min_citations=cfg.min_citations if min_citations is None else min_citations,
    )


def citation_count(page: Page) -> int:
    return len(page.claims) + len(page.sources)


def is_thin(page: Page, cfg: EnrichmentConfig) -> bool:
    """A page qualifies as thin under either threshold (issue #309)."""
    return len(page.body) < cfg.min_body_chars or citation_count(page) < cfg.min_citations


@dataclass
class EnrichResult:
    """Outcome of one `enrich_page` call: a filed proposal, or a skip reason."""

    page_id: str
    proposal_id: str | None = None
    claim_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "proposed": self.skipped_reason is None,
            "proposal_id": self.proposal_id,
            "claim_ids": self.claim_ids,
            "source_ids": self.source_ids,
            "skipped_reason": self.skipped_reason,
            "dry_run": self.dry_run,
        }


def _sources_for_claims(store: KBStore, claim_ids: list[str]) -> list[str]:
    """Evidence ids of the cited claims that resolve to registered Sources.

    `propose_page`'s `source_ids` are validated against `store.get_source`
    only (a claim's `evidence` list may also hold bare Evidence ids — see
    `models.Claim.evidence` — which aren't valid page sources), so this
    filters down to the subset that would actually pass that check.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in claim_ids:
        try:
            claim = store.get_claim(cid)
        except ArtifactNotFoundError:
            continue
        for eid in claim.evidence:
            if eid in seen:
                continue
            try:
                store.get_source(eid)
            except ArtifactNotFoundError:
                continue
            seen.add(eid)
            ordered.append(eid)
    return ordered


def enrich_page(
    store: KBStore,
    page_id: str,
    *,
    actor: str = ENRICH_ACTOR,
    min_body_chars: int | None = None,
    min_citations: int | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
    config: EnrichmentConfig | None = None,
) -> EnrichResult:
    """Score `page_id` against the thin-page thresholds and, if it
    qualifies, draft an enriched revision and file it as a page proposal.

    The addition is synthesized strictly from approved, non-retracted
    claims already in the KB (via `synthesize.synthesize`) and appended to
    the page's existing body, so a stub's hand-written text is never
    discarded. Skips (no proposal filed) when the page is already above the
    thresholds, or when no related approved claim exists to cite.
    """
    cfg = config or load_config(store)
    thresholds = _resolve_thresholds(
        cfg, min_body_chars=min_body_chars, min_citations=min_citations,
    )
    page = store.get_page(page_id)

    if not is_thin(page, thresholds):
        return EnrichResult(
            page_id=page_id, skipped_reason="page is above the thin-page thresholds",
        )

    query = f"{page.title}\n\n{page.body}".strip()
    result = synthesize(
        store, query=query, depth=_SYNTHESIZE_DEPTH, max_chars=_SYNTHESIZE_MAX_CHARS,
    )
    addition = str(result["answer"])
    new_claim_ids = [str(c) for c in result["claims"]]
    if not new_claim_ids:
        return EnrichResult(page_id=page_id, skipped_reason="no related approved claims found")

    claim_ids = list(dict.fromkeys([*page.claims, *new_claim_ids]))
    source_ids = list(dict.fromkeys([*page.sources, *_sources_for_claims(store, new_claim_ids)]))
    body = f"{page.body.strip()}\n\n{addition}".strip() if page.body.strip() else addition
    confidence = result["_meta"]["synthesis_confidence"]
    rationale = (
        f"enriched from {len(new_claim_ids)} approved claim(s) via kb.synthesize "
        f"(synthesis_confidence: {confidence})"
    )

    if dry_run:
        return EnrichResult(
            page_id=page_id, claim_ids=claim_ids, source_ids=source_ids, dry_run=True,
        )

    try:
        proposal = propose_page(
            store,
            title=page.title,
            body=body,
            page_type=page.type,
            claim_ids=claim_ids,
            entity_ids=list(page.entities),
            source_ids=source_ids,
            tags=list(page.tags),
            metadata=dict(page.metadata),
            proposed_by=actor,
            rationale=rationale,
            slug_hint=page.id,
            session_id=session_id,
        )
    except ProposalError as e:
        # e.g. a page kind's required fields tightened since the page was
        # last approved — one page's rejection must not sink a batch run.
        return EnrichResult(
            page_id=page_id, claim_ids=claim_ids, source_ids=source_ids,
            skipped_reason=str(e),
        )
    return EnrichResult(
        page_id=page_id, proposal_id=proposal.id, claim_ids=claim_ids, source_ids=source_ids,
    )


@dataclass
class EnrichPagesReport:
    """Outcome of one `enrich_pages` batch pass over `store.list_pages()`."""

    proposed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposed": self.proposed,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
        }


def enrich_pages(
    store: KBStore,
    *,
    actor: str = ENRICH_ACTOR,
    min_body_chars: int | None = None,
    min_citations: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    session_id: str | None = None,
    triggered_by: str | None = None,
) -> EnrichPagesReport:
    """Scan `store.list_pages()`, select pages under the thin-page
    thresholds, and file one `enrich_page` proposal per qualifying page.

    `limit` caps how many thin pages are processed in this run (a safety
    valve against filing a large batch of proposals in one pass), applied
    after thin-page selection. `dry_run` reports what would be proposed
    without filing anything.
    """
    cfg = load_config(store)
    thresholds = _resolve_thresholds(
        cfg, min_body_chars=min_body_chars, min_citations=min_citations,
    )
    thin_pages = [p for p in store.list_pages() if is_thin(p, thresholds)]
    if limit is not None:
        thin_pages = thin_pages[:limit]

    report = EnrichPagesReport(dry_run=dry_run)
    for page in thin_pages:
        result = enrich_page(
            store, page.id, actor=actor,
            session_id=session_id, dry_run=dry_run, config=thresholds,
        )
        if result.skipped_reason:
            report.skipped.append({"page_id": page.id, "reason": result.skipped_reason})
        else:
            report.proposed.append({
                "page_id": page.id,
                "proposal_id": result.proposal_id or "(dry-run)",
                "claim_ids": result.claim_ids,
                "source_ids": result.source_ids,
            })

    if not dry_run:
        audit_mod.log_event(
            store.kb_dir,
            event="enrich_pages.run",
            actor=triggered_by or actor,
            object_ids=[
                row["proposal_id"] for row in report.proposed
                if row["proposal_id"] != "(dry-run)"
            ],
            data={"proposed": len(report.proposed), "skipped": len(report.skipped)},
        )
    return report
