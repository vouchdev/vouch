"""Session-start recall digest — inject all approved knowledge into a new
Claude session's context (memvid-style), via the SessionStart hook.

Unlike ``kb.context`` (task-scoped retrieval), this emits EVERY live approved
claim as a compact ``[id] text`` line plus every approved page title, so a
fresh session is aware of the whole reviewed KB from the first turn. Page
bodies are fetched on demand with ``kb_read_page`` / ``kb_search``.

Size-guarded: if the digest would exceed ``max_chars`` it is truncated with an
explicit notice — never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from .context import _RETRACTED_CLAIM_STATUSES
from .storage import KBStore

DEFAULT_ENABLED = True
DEFAULT_MAX_CHARS = 12000

_OPEN_TAG = "<vouch-approved-knowledge>"
_CLOSE_TAG = "</vouch-approved-knowledge>"


@dataclass(frozen=True)
class RecallConfig:
    enabled: bool = DEFAULT_ENABLED
    max_chars: int = DEFAULT_MAX_CHARS


def load_config(store: KBStore) -> RecallConfig:
    """Read ``recall:`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return RecallConfig()
    if not isinstance(loaded, dict):
        return RecallConfig()
    raw = loaded.get("recall")
    if not isinstance(raw, dict):
        return RecallConfig()
    return RecallConfig(
        enabled=bool(raw.get("enabled", DEFAULT_ENABLED)),
        max_chars=int(raw.get("max_chars", DEFAULT_MAX_CHARS)),
    )


def build_digest(store: KBStore, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Return an injectable digest of every live approved claim + page title.

    Empty string when the KB has no approved knowledge (nothing to inject).
    """
    claims = [
        c for c in store.list_claims()
        if c.status not in _RETRACTED_CLAIM_STATUSES
    ]
    pages = store.list_pages()
    if not claims and not pages:
        return ""

    lines: list[str] = [
        _OPEN_TAG,
        f"# approved KB knowledge for this repo — {len(claims)} claim(s), "
        f"{len(pages)} page(s). reviewed, cited, durable. use kb_read_page / "
        "kb_search for detail; kb_propose_* (human-approved) to add more.",
    ]
    if claims:
        lines += ["", "## claims"]
        lines += [f"- [{c.id}] {c.text}" for c in claims]
    if pages:
        lines += ["", "## pages"]
        lines += [f"- [{p.id}] {p.title}" for p in pages]
    lines.append(_CLOSE_TAG)
    body = "\n".join(lines)

    if len(body) > max_chars:
        keep = max(0, max_chars - len(_CLOSE_TAG) - 120)
        notice = (
            f"\n… [truncated: approved KB exceeds {max_chars} chars; "
            "run `vouch search` / kb_context for the rest]\n" + _CLOSE_TAG
        )
        body = body[:keep].rstrip() + notice
    return body
