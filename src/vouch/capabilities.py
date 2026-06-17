"""Capabilities — what this server / CLI implementation supports.

Returned by `kb_capabilities` (MCP) and `vouch.capabilities` (JSONL). Lets
adapters classify vouch as an AKBP-compatible local cited review-gated KB
without hardcoding assumptions.
"""

from __future__ import annotations

from . import __version__
from .models import Capabilities

# The full method surface this implementation exposes. Keep this list in
# sync with the MCP server + JSONL server registrations — `test_capabilities`
# asserts they match.
METHODS = [
    "kb.capabilities",
    "kb.status",
    "kb.stats",
    "kb.search",
    "kb.context",
    "kb.read_page",
    "kb.read_claim",
    "kb.read_entity",
    "kb.read_relation",
    "kb.list_pages",
    "kb.list_claims",
    "kb.list_entities",
    "kb.list_relations",
    "kb.list_sources",
    "kb.list_pending",
    "kb.register_source",
    "kb.register_source_from_path",
    "kb.propose_claim",
    "kb.propose_page",
    "kb.propose_entity",
    "kb.propose_relation",
    "kb.approve",
    "kb.reject",
    "kb.reject_extracted",
    "kb.expire",
    "kb.supersede",
    "kb.contradict",
    "kb.archive",
    "kb.confirm",
    "kb.cite",
    "kb.source_verify",
    "kb.session_start",
    "kb.session_end",
    "kb.crystallize",
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
]


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
    )
