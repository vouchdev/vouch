"""Conversation-export importers — seed the review queue from prior agent history.

Each format reader normalizes an export dump into candidate claims/pages and
routes them through ``proposals.propose_*``. Nothing is auto-approved; the
review gate is unchanged (vouchdev/vouch#431).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .models import ProposalKind, ProposalStatus
from .proposals import ProposeClaimResult, propose_claim, propose_page
from .storage import KBStore

log = logging.getLogger(__name__)

ImportFormat = Literal["chat-json", "markdown-vault", "memory-export"]
IMPORT_FORMATS: tuple[ImportFormat, ...] = (
    "chat-json",
    "markdown-vault",
    "memory-export",
)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_CLAIM_MAX_CHARS = 500
_IMPORT_ACTOR = "vouch-import"


class CorpusImportError(RuntimeError):
    """Raised when an export path or payload cannot be parsed."""


@dataclass
class ImportCandidate:
    """Normalized item ready for propose-time routing."""

    kind: Literal["claim", "page"]
    text: str
    title: str | None = None
    body: str | None = None
    slug_hint: str | None = None
    tags: list[str] = field(default_factory=list)
    rationale: str | None = None
    source_path: Path | None = None


@dataclass
class ImportResult:
    """Outcome of one ``run_import`` call."""

    format: ImportFormat
    path: str
    dry_run: bool
    claims_proposed: int = 0
    pages_proposed: int = 0
    claims_skipped_dedup: int = 0
    pages_skipped_dedup: int = 0
    cap_hit: bool = False
    proposal_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _slugify(text: str) -> str:
    out: list[str] = []
    last_dash = False
    for ch in text.lower().strip():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    slug = "".join(out).strip("-")
    return slug[:60] or "untitled"


# --- shared markdown extraction (#321 core) ---------------------------------


def iter_markdown_files(root: Path) -> Iterator[Path]:
    """Yield ``*.md`` files under *root*, sorted for deterministic runs."""
    if root.is_file():
        if root.suffix.lower() == ".md":
            yield root
        return
    for path in sorted(root.rglob("*.md")):
        if path.is_file():
            yield path


def parse_markdown_file(path: Path) -> tuple[str, str, dict[str, Any]]:
    """Return ``(title, body, frontmatter)`` for one markdown file."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(text)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
        body = m.group(2)
        title = str(meta.get("title") or path.stem)
        return title, body, meta
    return path.stem, text, {}


def candidates_from_markdown_vault(root: Path) -> list[ImportCandidate]:
    out: list[ImportCandidate] = []
    for md_path in iter_markdown_files(root):
        title, body, meta = parse_markdown_file(md_path)
        slug = meta.get("id")
        slug_hint = str(slug) if slug else _slugify(title)
        tags_raw = meta.get("tags")
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        out.append(ImportCandidate(
            kind="page",
            text=title,
            title=title,
            body=body,
            slug_hint=slug_hint,
            tags=tags,
            rationale=f"imported from {md_path.name}",
            source_path=md_path,
        ))
    return out


# --- chat-json --------------------------------------------------------------


def _message_content(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p).strip()
    return str(raw).strip()


def _iter_chat_messages(data: Any) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Yield ``(conversation_title, message)`` from supported chat-json shapes."""
    if isinstance(data, list):
        for msg in data:
            if isinstance(msg, dict):
                yield None, msg
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("messages"), list):
        for msg in data["messages"]:
            if isinstance(msg, dict):
                yield str(data["title"]) if data.get("title") else None, msg
        return
    conversations = data.get("conversations")
    if isinstance(conversations, list):
        for conv in conversations:
            if not isinstance(conv, dict):
                continue
            title = conv.get("title")
            messages = conv.get("messages")
            if isinstance(messages, list):
                for msg in messages:
                    if isinstance(msg, dict):
                        yield str(title) if title else None, msg


def candidates_from_chat_json(path: Path) -> list[ImportCandidate]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CorpusImportError(f"cannot parse chat-json at {path}: {e}") from e

    out: list[ImportCandidate] = []
    idx = 0
    for conv_title, msg in _iter_chat_messages(data):
        role = str(msg.get("role") or msg.get("author") or "").lower()
        if role not in {"assistant", "model", "ai"}:
            continue
        content = _message_content(msg.get("content") or msg.get("text"))
        if not content:
            continue
        idx += 1
        title_base = conv_title or f"message-{idx}"
        if len(content) <= _CLAIM_MAX_CHARS:
            out.append(ImportCandidate(
                kind="claim",
                text=content,
                rationale=f"imported assistant message from {path.name}",
            ))
        else:
            first_line = content.split("\n", 1)[0].strip("# ").strip() or title_base
            out.append(ImportCandidate(
                kind="page",
                text=first_line[:120],
                title=first_line[:120],
                body=content,
                rationale=f"imported assistant message from {path.name}",
            ))
    if not out:
        raise CorpusImportError(f"no assistant messages found in chat-json: {path}")
    return out


# --- memory-export ----------------------------------------------------------


def _iter_memory_entries(data: Any) -> Iterator[str]:
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item.strip():
                yield item.strip()
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("memory")
                if isinstance(text, str) and text.strip():
                    yield text.strip()
        return
    if not isinstance(data, dict):
        return
    for key in ("memories", "entries", "items", "data"):
        bucket = data.get(key)
        if isinstance(bucket, list):
            yield from _iter_memory_entries(bucket)
            return


def candidates_from_memory_export(path: Path) -> list[ImportCandidate]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CorpusImportError(f"cannot parse memory-export at {path}: {e}") from e

    out = [
        ImportCandidate(
            kind="claim",
            text=text,
            rationale=f"imported memory from {path.name}",
        )
        for text in _iter_memory_entries(data)
    ]
    if not out:
        raise CorpusImportError(f"no memory entries found in {path}")
    return out


def load_candidates(format: ImportFormat, path: Path) -> list[ImportCandidate]:
    if format == "markdown-vault":
        candidates = candidates_from_markdown_vault(path)
        if not candidates:
            raise CorpusImportError(f"no markdown files found under {path}")
        return candidates
    if format == "chat-json":
        return candidates_from_chat_json(path)
    return candidates_from_memory_export(path)


# --- dedup + propose routing ------------------------------------------------


def _claim_is_duplicate(store: KBStore, text: str) -> bool:
    try:
        from .embeddings.similarity import find_similar_on_propose

        warnings = find_similar_on_propose(store, text)
    except ImportError:
        return False
    return any(w.get("code") == "similar_approved" for w in warnings)


def _page_is_duplicate(store: KBStore, slug: str) -> bool:
    if (store.kb_dir / "pages" / f"{slug}.md").exists():
        return True
    for prop in store.list_proposals(ProposalStatus.PENDING):
        if prop.kind == ProposalKind.PAGE and prop.payload.get("id") == slug:
            return True
    return False


def _register_file_source(store: KBStore, file_path: Path, *, root: Path | None = None) -> str:
    body = file_path.read_bytes()
    locator: str
    if root is not None:
        try:
            rel = file_path.resolve().relative_to(root.resolve())
            locator = f"import:{rel.as_posix()}"
        except ValueError:
            locator = f"import:{file_path.name}"
    else:
        locator = f"import:{file_path.resolve()}"
    media = "application/json" if file_path.suffix.lower() == ".json" else "text/markdown"
    src = store.put_source(
        body,
        title=file_path.name,
        locator=locator,
        source_type="file",
        media_type=media,
    )
    return src.id


def run_import(
    store: KBStore,
    format: ImportFormat,
    path: Path,
    *,
    dry_run: bool = False,
    max_proposals: int | None = None,
    actor: str = _IMPORT_ACTOR,
) -> ImportResult:
    """Parse *path* and enqueue proposals via ``propose_*`` (never approve)."""
    resolved = path.resolve()
    if not resolved.exists():
        raise CorpusImportError(f"path not found: {path}")

    candidates = load_candidates(format, resolved)
    result = ImportResult(format=format, path=str(resolved), dry_run=dry_run)

    json_source_id: str | None = None
    if format in {"chat-json", "memory-export"}:
        json_source_id = _register_file_source(store, resolved)

    vault_root = resolved if resolved.is_dir() else resolved.parent
    proposed = 0
    cap = max_proposals if max_proposals is not None and max_proposals >= 0 else None

    for candidate in candidates:
        if cap is not None and proposed >= cap:
            result.cap_hit = True
            break

        if candidate.kind == "claim":
            if _claim_is_duplicate(store, candidate.text):
                result.claims_skipped_dedup += 1
                continue
            if json_source_id is None:
                raise CorpusImportError("internal error: json import missing source id")
            claim_result: ProposeClaimResult = propose_claim(
                store,
                text=candidate.text,
                evidence=[json_source_id],
                proposed_by=actor,
                tags=candidate.tags,
                rationale=candidate.rationale,
                slug_hint=candidate.slug_hint,
                dry_run=dry_run,
            )
            result.proposal_ids.append(claim_result.id)
            for w in claim_result.warnings:
                if w.get("code") == "similar_pending":
                    result.warnings.append(
                        f"claim similar to pending {w.get('artifact_id')}"
                    )
            result.claims_proposed += 1
            proposed += 1
            continue

        title = candidate.title or candidate.text
        slug = candidate.slug_hint or _slugify(title)
        if _page_is_duplicate(store, slug):
            result.pages_skipped_dedup += 1
            continue

        source_ids: list[str] = []
        if candidate.source_path is not None:
            source_ids = [_register_file_source(store, candidate.source_path, root=vault_root)]

        page = propose_page(
            store,
            title=title,
            body=candidate.body or "",
            page_type="concept",
            source_ids=source_ids,
            tags=candidate.tags,
            rationale=candidate.rationale,
            slug_hint=slug,
            proposed_by=actor,
            dry_run=dry_run,
        )
        result.proposal_ids.append(page.id)
        result.pages_proposed += 1
        proposed += 1

    return result
