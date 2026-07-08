"""Frontmatter-aware filtering for page listings.

Typed record kinds (see the company-brain template in `onboarding.py`) put
their structure in `Page.metadata` — `due_at` on a followup, `record_status`
on a project record. This module gives every listing surface one shared,
deliberately small filter vocabulary over that frontmatter: kind equality,
per-field equality, and inclusive ordered bounds. Anything richer belongs in
the caller — this is a viewport over `store.list_pages()`, not a query
language, and the yaml files stay the only source of truth.

Ordered comparisons try numbers first and fall back to string comparison,
which orders ISO-8601 dates correctly. A page missing a filtered field never
matches: filters select records that positively satisfy the predicate.
"""

from __future__ import annotations

from typing import Any

from .models import Page


def filter_pages(
    pages: list[Page],
    *,
    kind: str | None = None,
    equals: dict[str, str] | None = None,
    before: dict[str, str] | None = None,
    after: dict[str, str] | None = None,
) -> list[Page]:
    """Return the pages matching every given predicate.

    kind: exact match on `Page.type`.
    equals: frontmatter field == value (string-compared).
    before / after: inclusive bounds — value <= / >= the given bound.
    """
    out: list[Page] = []
    for page in pages:
        if kind is not None and page.type != kind:
            continue
        meta = page.metadata
        if equals and not all(_eq(meta.get(k), v) for k, v in equals.items()):
            continue
        if before and not all(_lte(meta.get(k), v) for k, v in before.items()):
            continue
        if after and not all(_lte(v, meta.get(k)) for k, v in after.items()):
            continue
        out.append(page)
    return out


def parse_kv(pairs: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Parse repeated ``key=value`` CLI arguments; raise ValueError on malformed."""
    parsed: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"expected key=value, got {pair!r}")
        parsed[key] = value
    return parsed


def _eq(value: Any, bound: str) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value == bound
    return str(value) == bound


def _lte(value: Any, bound: Any) -> bool:
    """value <= bound; numeric when both sides parse as numbers, else string.

    String comparison orders ISO-8601 timestamps correctly, which is what
    date-bearing frontmatter (``due_at``) uses.
    """
    if value is None or bound is None:
        return False
    try:
        return float(value) <= float(bound)
    except (TypeError, ValueError):
        return str(value) <= str(bound)
