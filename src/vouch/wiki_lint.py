"""kb.wiki_lint - page-health sweep over the approved wiki (roadmap 1.4).

``health.lint()`` finds user-actionable problems in claims: broken
citations, stale claims, dangling refs. Nothing plays that role for pages -
a page's ``[[wikilinks]]`` are validated once, at compile-draft time
(``compile.py``'s ``_first_dangling_link`` / ``_citation_coverage``), and
never re-checked once the page is approved and living on disk. A page whose
linked target is later archived, or whose citation coverage erodes after a
hand edit, or that nothing in the wiki links to, surfaces nowhere.

This is the page-level sibling of ``health.lint()``, reusing the same
building blocks against the *live*, already-approved page set instead of a
single draft: ``wiki_render``'s link index/backlinks for the wiki graph, and
``compile``'s citation-coverage sentence walk for prose quality. Four
checks:

* ``orphan_page`` - no other page's ``[[wikilink]]`` points at it.
* ``dead_wikilink`` - a ``[[link]]`` in its body doesn't resolve to any live
  page (title, id/slug, or alias).
* ``stale_page`` - not updated in ``stale_after_days``.
* ``uncited_section`` - citation coverage has dropped below
  ``min_citation_coverage`` (the same guardrail ``compile.py`` enforces on a
  *draft* before it's ever proposed, re-run here against the *approved* body,
  which review/hand-editing can erode without re-triggering that gate).

Read-only: never proposes, writes, or mutates anything - a derived view over
approved pages, like ``wiki_render``'s index/backlinks/MOC, not a write path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .compile import _citation_coverage
from .health import Finding, HealthReport
from .models import Page, PageStatus
from .storage import KBStore
from .wiki_render import _WIKILINK_RE, backlinks, resolve_link

DEFAULT_STALE_AFTER_DAYS = 180
DEFAULT_MIN_CITATION_COVERAGE = 0.5


def _live_pages(store: KBStore) -> list[Page]:
    # Same live set as render-wiki / recall / digest / search - archived
    # pages stay on disk but are out of the wiki front door (#695), so a link
    # to one is exactly as dead as a link to nothing.
    return [p for p in store.list_pages() if p.status is not PageStatus.ARCHIVED]


def wiki_lint(
    store: KBStore,
    *,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
    min_citation_coverage: float = DEFAULT_MIN_CITATION_COVERAGE,
) -> HealthReport:
    """Page-health sweep: orphan pages, dead wikilinks, stale pages, thin citation coverage."""
    pages = _live_pages(store)
    inbound = backlinks(pages)
    findings: list[Finding] = []
    now = datetime.now(UTC)

    for page in pages:
        if not inbound.get(page.id):
            findings.append(
                Finding(
                    "info",
                    "orphan_page",
                    f"page {page.id} has no inbound [[wikilinks]]",
                    [page.id],
                )
            )

        seen_dead: set[str] = set()
        for raw in _WIKILINK_RE.findall(page.body):
            target = raw.strip()
            key = target.lower()
            if key in seen_dead:
                continue
            if resolve_link(target, pages) is None:
                seen_dead.add(key)
                findings.append(
                    Finding(
                        "warning",
                        "dead_wikilink",
                        f"page {page.id} links to unresolved [[{target}]]",
                        [page.id],
                    )
                )

        anchor = page.updated_at or page.created_at
        if anchor is not None:
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=UTC)
            if (now - anchor) > timedelta(days=stale_after_days):
                findings.append(
                    Finding(
                        "warning",
                        "stale_page",
                        f"page {page.id} not updated in >{stale_after_days}d",
                        [page.id],
                    )
                )

        cited, total = _citation_coverage(page.body)
        if total > 0:
            coverage = cited / total
            if coverage < min_citation_coverage:
                findings.append(
                    Finding(
                        "warning",
                        "uncited_section",
                        f"page {page.id} citation coverage {coverage:.0%} "
                        f"below minimum {min_citation_coverage:.0%}",
                        [page.id],
                    )
                )

    ok = not any(f.severity == "error" for f in findings)
    return HealthReport(ok=ok, findings=findings, counts={"pages_checked": len(pages)})
