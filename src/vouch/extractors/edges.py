"""Auto-extracted typed edges for approved pages (issue #224).

Parses an approved page for `[[entity-id]]` wiki-links and reuses the
`entities` / `sources` frontmatter lists `propose_page` already validated,
then files each implied relationship as an ordinary relation proposal.
`proposed_by=AUTO_EXTRACTOR_ACTOR` is the only thing that distinguishes an
extracted edge from a hand-filed one -- it still lands in `proposed/` and
needs a reviewer's approve/reject like any other write.
"""

from __future__ import annotations

import re

from ..models import Page, Proposal, RelationType
from ..proposals import ProposalError, propose_relation
from ..storage import KBStore

AUTO_EXTRACTOR_ACTOR = "vouch-extractor"

_WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9][A-Za-z0-9._-]*)\]\]")


def extract_wikilinks(body: str) -> list[str]:
    """Distinct `[[entity-id]]` targets in `body`, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _WIKILINK_RE.finditer(body):
        seen.setdefault(match.group(1), None)
    return list(seen)


def auto_propose_edges(
    store: KBStore,
    page: Page,
    *,
    proposed_by: str = AUTO_EXTRACTOR_ACTOR,
    session_id: str | None = None,
) -> list[Proposal]:
    """File relation proposals implied by an approved page's links.

    Three edge kinds, one per existing page field:
      - `mentions`     -- `[[wiki-links]]` found in the page body
      - `relates_to`   -- the page's `entities` frontmatter list
      - `derived_from` -- the page's `sources` frontmatter list

    Edges are proposed independently of each other; a proposal that fails
    (e.g. an empty endpoint) is skipped rather than blocking the rest or
    the page approval that triggered extraction.
    """
    edges: list[tuple[RelationType, str]] = []
    for target in extract_wikilinks(page.body):
        if target != page.id:
            edges.append((RelationType.MENTIONS, target))
    for entity_id in page.entities:
        if entity_id != page.id:
            edges.append((RelationType.RELATES_TO, entity_id))
    for source_id in page.sources:
        edges.append((RelationType.DERIVED_FROM, source_id))

    proposals: list[Proposal] = []
    for relation, target in edges:
        try:
            proposals.append(
                propose_relation(
                    store,
                    src=page.id,
                    relation=relation.value,
                    target=target,
                    proposed_by=proposed_by,
                    confidence=0.5,
                    rationale=f"auto-extracted from page {page.id}",
                    session_id=session_id,
                )
            )
        except ProposalError:
            continue
    return proposals
