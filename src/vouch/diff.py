"""Claim/page revision diff — `vouch diff <id-old> <id-new>`.

Read-only: shows what changed between two claim revisions or two page
revisions. Field-level changes for scalars/lists, plus a line-diff of the long
text field (`claim.text` / `page.body`). No writes, no proposals, no audit.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .models import Claim, Page
from .storage import ArtifactNotFoundError, KBStore

# (long-text field rendered as a line diff, scalar/list fields shown as old→new)
_CLAIM_TEXT = "text"
_CLAIM_FIELDS = [
    "type", "status", "confidence", "evidence", "entities", "tags",
    "supersedes", "superseded_by", "contradicts", "scope",
]
_PAGE_TEXT = "body"
_PAGE_FIELDS = ["title", "type", "status", "claims", "entities", "sources", "tags"]


class DiffError(ValueError):
    """Raised for unknown ids or mismatched artifact kinds.

    Subclasses ValueError so the CLI's `_cli_errors()` renders it as a clean
    `Error:` line instead of a traceback.
    """


@dataclass
class FieldChange:
    field: str
    old: Any
    new: Any


@dataclass
class ArtifactDiff:
    kind: str
    old_id: str
    new_id: str
    changes: list[FieldChange]
    text_diff: list[str]


def _kind_of(store: KBStore, artifact_id: str) -> str | None:
    try:
        store.get_claim(artifact_id)
        return "claim"
    except ArtifactNotFoundError:
        pass
    try:
        store.get_page(artifact_id)
        return "page"
    except ArtifactNotFoundError:
        return None


def _norm(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _line_diff(old: str, new: str) -> list[str]:
    return list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="",
    ))


def diff_artifacts(store: KBStore, old_id: str, new_id: str) -> ArtifactDiff:
    """Diff two same-kind artifacts (both claims or both pages) by id."""
    old_kind = _kind_of(store, old_id)
    if old_kind is None:
        raise DiffError(f"unknown artifact: {old_id}")
    new_kind = _kind_of(store, new_id)
    if new_kind is None:
        raise DiffError(f"unknown artifact: {new_id}")
    if old_kind != new_kind:
        raise DiffError(f"cannot diff {old_kind} against {new_kind}")

    old: Claim | Page
    new: Claim | Page
    if old_kind == "claim":
        old, new = store.get_claim(old_id), store.get_claim(new_id)
        fields, text_field = _CLAIM_FIELDS, _CLAIM_TEXT
    else:
        old, new = store.get_page(old_id), store.get_page(new_id)
        fields, text_field = _PAGE_FIELDS, _PAGE_TEXT

    changes: list[FieldChange] = []
    for field in fields:
        o, n = _norm(getattr(old, field)), _norm(getattr(new, field))
        if o != n:
            changes.append(FieldChange(field=field, old=o, new=n))

    text_diff = _line_diff(getattr(old, text_field), getattr(new, text_field))
    return ArtifactDiff(
        kind=old_kind, old_id=old_id, new_id=new_id,
        changes=changes, text_diff=text_diff,
    )
