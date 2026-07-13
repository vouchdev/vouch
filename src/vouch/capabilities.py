"""Capabilities — what this server / CLI implementation supports.

Returned by `kb_capabilities` (MCP) and `vouch.capabilities` (JSONL). Lets
adapters classify vouch as an AKBP-compatible local cited review-gated KB
without hardcoding assumptions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import __version__
from .models import Capabilities
from .openclaw.context_engine import describe_engine

_log = logging.getLogger(__name__)

# Path to package.json, relative to this module. capabilities.py lives at
# src/vouch/capabilities.py; package.json lives at the repo root, three levels
# up (src/vouch/ -> src/ -> repo root). The openclaw.compat block lives here,
# not in openclaw.plugin.json — the manifest bans openclaw.* dead dialect
# fields (see test_manifest_carries_no_dead_dialect_fields).
_PACKAGE_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "package.json"

# The full method surface this implementation exposes. Keep this list in
# sync with the MCP server + JSONL server registrations — `test_capabilities`
# asserts they match.
METHODS = [
    "kb.capabilities",
    "kb.status",
    "kb.stats",
    "kb.activity",
    "kb.digest",
    "kb.search",
    "kb.neighbors",
    "kb.context",
    "kb.synthesize",
    "kb.read_page",
    "kb.read_claim",
    "kb.read_entity",
    "kb.read_relation",
    "kb.diff",
    "kb.list_pages",
    "kb.list_claims",
    "kb.list_entities",
    "kb.list_relations",
    "kb.list_sources",
    "kb.list_pending",
    "kb.triage_pending",
    "kb.register_source",
    "kb.register_source_from_path",
    "kb.propose_claim",
    "kb.propose_page",
    "kb.propose_entity",
    "kb.propose_relation",
    "kb.propose_delete",
    "kb.approve",
    "kb.reject",
    "kb.reject_extracted",
    "kb.expire",
    "kb.supersede",
    "kb.contradict",
    "kb.archive",
    "kb.confirm",
    "kb.clear_claims",
    "kb.cite",
    "kb.source_verify",
    "kb.session_start",
    "kb.session_end",
    "kb.list_sessions",
    "kb.session_transcript",
    "kb.volunteer_context",
    "kb.crystallize",
    "kb.summarize_session",
    "kb.index_rebuild",
    "kb.lint",
    "kb.doctor",
    "kb.export",
    "kb.export_check",
    "kb.import_check",
    "kb.import_apply",
    "kb.audit",
    "kb.reindex_embeddings",
    "kb.dedup_scan",
    "kb.eval_embeddings",
    "kb.embeddings_stats",
    "kb.why",
    "kb.trace",
    "kb.impact",
    "kb.graph_export",
    "kb.provenance_rebuild",
    "kb.detect_themes",
    "kb.propose_theme",
    "kb.compile",
]


def _load_host_compat() -> dict[str, dict[str, str]]:
    """Read the `openclaw.compat` block from package.json (#237).

    Surfaced in `kb.capabilities` as `host_compat` so non-OpenClaw clients
    can detect compat without parsing package.json themselves. Returns an
    empty dict (rather than raising) if package.json is missing or
    malformed — capabilities() must never fail to report basic info just
    because the file moved or this is installed as a standalone wheel
    without package.json packaged alongside it.
    """
    try:
        manifest = json.loads(_PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.debug("package.json unreadable, host_compat will be empty: %s", e)
        return {}
    compat = manifest.get("openclaw", {}).get("compat")
    if not isinstance(compat, dict):
        return {}
    return {"openclaw": {k: str(v) for k, v in compat.items()}}


def capabilities() -> Capabilities:
    retrieval = ["fts5", "substring"]
    try:
        from .embeddings import get_embedder
        get_embedder()
        retrieval.append("embedding")
        retrieval.append("hybrid")
    except Exception:
        pass
    return Capabilities(
        version=__version__,
        methods=METHODS,
        retrieval=retrieval,
        review_gated=True,
        transports=["mcp", "jsonl", "http"],
        scoping={
            "enabled": True,
            "viewer_params": ["project", "agent"],
            "env_vars": ["VOUCH_PROJECT", "VOUCH_AGENT"],
            "config_path": "retrieval.scope",
        },
        context_engines=[describe_engine()],
        host_compat=_load_host_compat(),
    )
