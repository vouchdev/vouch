"""MCP server exposing the full kb.* tool surface to LLM agents.

Tools are grouped by intent: read tools are unrestricted; write tools file
proposals (the review gate); lifecycle tools (supersede/contradict/archive)
mutate durable claims directly because they are metadata about reviewed
knowledge, not new assertions. The audit log captures everything.

`VOUCH_AGENT` is the agent identifier recorded on every proposal /
audit event, so multi-agent setups can attribute writes correctly.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

from . import audit, bundle, health, volunteer_context
from . import lifecycle as life
from . import salience as salience_mod
from . import sessions as sess_mod
from . import trust as trust_mod
from . import verify as verify_mod
from .capabilities import capabilities as build_caps
from .context import build_context_pack
from .logging_config import configure_logging
from .models import ProposalStatus
from .proposals import (
    EXPIRE_ACTOR,
    ProposalError,
    approve,
    expire_pending,
    propose_claim,
    propose_entity,
    propose_page,
    propose_relation,
    reject,
    reject_auto_extracted,
)
from .scoping import filter_hits, scoped_fetch_limit, viewer_from
from .stats import collect_stats
from .storage import (
    ArtifactNotFoundError,
    KBNotFoundError,
    KBStore,
    discover_root,
)
from .synthesize import synthesize

mcp = FastMCP("vouch")


def _store() -> KBStore:
    try:
        return KBStore(discover_root())
    except KBNotFoundError as e:
        raise RuntimeError(
            f"{e}. Run `vouch init` in the project root before starting the server."
        ) from e


def _agent() -> str:
    return os.environ.get("VOUCH_AGENT", "unknown-agent")


# === capabilities / status ================================================


@mcp.tool()
def kb_capabilities() -> dict[str, Any]:
    """Return the protocol capabilities of this server."""
    return build_caps().model_dump(mode="json")


@mcp.tool()
def kb_status() -> dict[str, Any]:
    """Return KB artifact counts and health summary."""
    return health.status(_store())


@mcp.tool()
def kb_stats(*, days: int = 30) -> dict[str, Any]:
    """Observability: pending by agent, review rates, citation coverage.

    days: decision window in days; 0 means all-time.
    """
    since = None if days == 0 else days
    return collect_stats(_store(), since_days=since)


# === read tools (unrestricted) ============================================


@mcp.tool()
def kb_search(
    query: str,
    *,
    limit: int = 10,
    backend: str = "auto",
    min_score: float = 0.0,
    project: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Search the KB.

    backend: "auto" (default, embedding then fts5 then substring),
    "embedding", "fts5", "substring", or "hybrid".
    project/agent: optional viewer context for scope filtering.
    """
    from . import index_db
    store = _store()
    viewer = viewer_from(
        config_path=store.config_path,
        project=project,
        agent=agent,
    )
    fetch_limit = scoped_fetch_limit(limit, viewer)
    hits: list[tuple[str, str, str, float]] = []

    def _to_dicts(h: list[tuple[str, str, str, float]], used: str) -> dict[str, Any]:
        scoped = filter_hits(store, h, viewer, limit=limit)
        return {
            "backend": used,
            "viewer": {"project": viewer.project, "agent": viewer.agent},
            "hits": [
                {"kind": k, "id": i, "snippet": sn, "score": sc, "backend": used}
                for k, i, sn, sc in scoped
            ],
        }

    if backend in ("auto", "embedding"):
        hits = index_db.search_semantic(
            store.kb_dir, query, limit=fetch_limit, min_score=min_score,
        )
        if hits:
            return _to_dicts(hits, "embedding")
        if backend == "embedding":
            return _to_dicts([], "embedding")

    if backend in ("auto", "fts5"):
        try:
            hits = index_db.search(store.kb_dir, query, limit=fetch_limit)
        except Exception:
            hits = []
        if hits:
            return _to_dicts(hits, "fts5")
        if backend == "fts5":
            return _to_dicts([], "fts5")

    if backend in ("auto", "substring"):
        hits = store.search_substring(query, limit=fetch_limit)
        return _to_dicts(hits, "substring")

    if backend == "hybrid":
        from .embeddings.fusion import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
            rrf_fuse,
        )
        # Hybrid must honour min_score (the embedding side can return
        # low-relevance noise otherwise) and survive FTS failures the same
        # way the dedicated fts5 branch does.
        emb = index_db.search_semantic(
            store.kb_dir, query, limit=fetch_limit * 2, min_score=min_score,
        )
        try:
            fts = index_db.search(store.kb_dir, query, limit=fetch_limit * 2)
        except Exception:
            fts = []
        hits = rrf_fuse(emb, fts, limit=fetch_limit)
        return _to_dicts(hits, "hybrid")

    raise ValueError(f"unknown backend: {backend}")


def _load_cfg(store: KBStore) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load((store.kb_dir / "config.yaml").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


@mcp.tool()
def kb_neighbors(
    node_id: str,
    depth: int = 1,
    rel_types: list[str] | None = None,
    max_nodes: int = 50,
) -> dict[str, Any]:
    """Return graph neighbors of a claim, page, entity, or source."""
    from .graph import find_neighbors

    try:
        return find_neighbors(
            _store(), node_id, depth=depth, rel_types=rel_types, max_nodes=max_nodes,
        )
    except ArtifactNotFoundError as e:
        raise ValueError(str(e)) from e


@mcp.tool()
def kb_context(
    task: str,
    limit: int = 10,
    max_chars: int | None = None,
    min_items: int = 0,
    require_citations: bool = False,
    session_id: str | None = None,
    project: str | None = None,
    agent: str | None = None,
    expand_graph: bool = False,
    graph_depth: int = 1,
    graph_limit: int = 20,
) -> dict[str, Any]:
    """Build a ContextPack ready to inject into an agent prompt.

    When ``session_id`` is given, the entity-salience reflex records the query
    and may attach ``_meta.vouch_salience`` (see ``salience`` module).
    """
    store = _store()
    cfg = _load_cfg(store)
    if session_id:
        _, window, _ = salience_mod.reflex_cfg(cfg)
        salience_mod.record_query(session_id, task, window=window)
    result: dict[str, Any] = build_context_pack(  # type: ignore[assignment]
        store, query=task, limit=limit, max_chars=max_chars,
        min_items=min_items, require_citations=require_citations,
        project=project, agent=agent,
        expand_graph=expand_graph, graph_depth=graph_depth, graph_limit=graph_limit,
    )
    return salience_mod.attach_salience(result, store, session_id, cfg)


@mcp.tool()
def kb_synthesize(
    query: str,
    depth: int = 3,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """Answer a query from approved claims only, with inline `[claim_id]`
    citations, an explicit gaps block, and a synthesis_confidence grade.

    Unlike `kb_context` (a ranked list), this returns prose where every
    sentence is traceable to an approved claim.
    """
    return synthesize(_store(), query=query, depth=depth, max_chars=max_chars)


@mcp.tool()
def kb_read_page(page_id: str) -> dict[str, Any]:
    """Return a page (title, body, claim ids)."""
    try:
        return _store().get_page(page_id).model_dump(mode="json")
    except ArtifactNotFoundError as e:
        raise ValueError(str(e)) from e


@mcp.tool()
def kb_read_claim(claim_id: str) -> dict[str, Any]:
    """Return a claim with its citation list."""
    try:
        return _store().get_claim(claim_id).model_dump(mode="json")
    except ArtifactNotFoundError as e:
        raise ValueError(str(e)) from e


@mcp.tool()
def kb_read_entity(entity_id: str) -> dict[str, Any]:
    try:
        return _store().get_entity(entity_id).model_dump(mode="json")
    except ArtifactNotFoundError as e:
        raise ValueError(str(e)) from e


@mcp.tool()
def kb_read_relation(relation_id: str) -> dict[str, Any]:
    try:
        return _store().get_relation(relation_id).model_dump(mode="json")
    except ArtifactNotFoundError as e:
        raise ValueError(str(e)) from e


@mcp.tool()
def kb_list_pages() -> list[dict[str, Any]]:
    return [
        {"id": p.id, "title": p.title, "type": p.type, "tags": p.tags}
        for p in _store().list_pages()
    ]


@mcp.tool()
def kb_list_claims(status: str | None = None) -> list[dict[str, Any]]:
    """List all claims, optionally filtered by status."""
    claims = _store().list_claims()
    if status:
        claims = [c for c in claims if c.status.value == status]
    return [c.model_dump(mode="json") for c in claims]


@mcp.tool()
def kb_list_entities(entity_type: str | None = None) -> list[dict[str, Any]]:
    entities = _store().list_entities()
    if entity_type:
        entities = [e for e in entities if e.type.value == entity_type]
    return [e.model_dump(mode="json") for e in entities]


@mcp.tool()
def kb_list_relations(node_id: str | None = None) -> list[dict[str, Any]]:
    """List all relations; if node_id is given, only edges touching it."""
    store = _store()
    rels = store.list_relations()
    if node_id:
        rels = [r for r in rels if r.source == node_id or r.target == node_id]
    return [r.model_dump(mode="json") for r in rels]


@mcp.tool()
def kb_list_sources() -> list[dict[str, Any]]:
    return [
        {"id": s.id, "title": s.title, "type": s.type.value,
         "locator": s.locator, "byte_size": s.byte_size}
        for s in _store().list_sources()
    ]


@mcp.tool()
def kb_list_pending() -> list[dict[str, Any]]:
    """List proposals awaiting human review."""
    return [
        p.model_dump(mode="json")
        for p in _store().list_proposals(ProposalStatus.PENDING)
    ]


# === write tools — gated (produce proposals) =============================


@mcp.tool()
def kb_register_source(
    content: str,
    title: str | None = None,
    url: str | None = None,
    source_type: str = "file",
    media_type: str = "text/plain",
) -> dict[str, Any]:
    """Register a source. Evidence intake is NOT gated (registering raw
    evidence is harmless and de-duplicates by content hash)."""
    src = _store().put_source(
        content.encode("utf-8"),
        title=title, url=url,
        locator=url or title or "inline",
        source_type=source_type,
        media_type=media_type,
    )
    audit.log_event(_store().kb_dir, event="source.add", actor=_agent(),
                    object_ids=[src.id])
    return src.model_dump(mode="json")


@mcp.tool()
def kb_register_source_from_path(path: str, title: str | None = None,
                                 url: str | None = None,
                                 source_type: str = "file") -> dict[str, Any]:
    s = _store()
    p, body = s.read_under_root(path)
    src = s.put_source(
        body, title=title or p.name, url=url,
        locator=str(p), source_type=source_type,
    )
    audit.log_event(s.kb_dir, event="source.add", actor=_agent(),
                    object_ids=[src.id])
    return src.model_dump(mode="json")


@mcp.tool()
def kb_propose_claim(
    text: str,
    evidence: list[str],
    claim_type: str = "observation",
    confidence: float = 0.7,
    entities: list[str] | None = None,
    rationale: str | None = None,
    tags: list[str] | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Propose a new claim. Becomes durable only after `kb_approve`."""
    try:
        result = propose_claim(
            _store(), text=text, evidence=evidence,
            claim_type=claim_type, confidence=confidence,
            entities=entities, tags=tags, rationale=rationale,
            slug_hint=slug_hint, session_id=session_id,
            dry_run=dry_run, proposed_by=_agent(),
        )
    except (ProposalError, ArtifactNotFoundError, ValueError) as e:
        raise ValueError(str(e)) from e
    return _proposal_response(result, dry_run)


@mcp.tool()
def kb_propose_page(
    title: str,
    body: str,
    page_type: str = "concept",
    claim_ids: list[str] | None = None,
    entity_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    try:
        pr = propose_page(
            _store(), title=title, body=body, page_type=page_type,
            claim_ids=claim_ids, entity_ids=entity_ids, source_ids=source_ids,
            metadata=metadata, rationale=rationale, slug_hint=slug_hint,
            session_id=session_id, dry_run=dry_run, proposed_by=_agent(),
        )
    except (ProposalError, ArtifactNotFoundError, ValueError) as e:
        raise ValueError(str(e)) from e
    return _proposal_response(pr, dry_run)


@mcp.tool()
def kb_propose_entity(
    name: str,
    entity_type: str,
    aliases: list[str] | None = None,
    description: str | None = None,
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    try:
        pr = propose_entity(
            _store(), name=name, entity_type=entity_type,
            aliases=aliases, description=description,
            rationale=rationale, slug_hint=slug_hint,
            session_id=session_id, dry_run=dry_run,
            proposed_by=_agent(),
        )
    except (ProposalError, ArtifactNotFoundError, ValueError) as e:
        raise ValueError(str(e)) from e
    return _proposal_response(pr, dry_run)


@mcp.tool()
def kb_propose_relation(
    src: str,
    relation: str,
    target: str,
    confidence: float = 0.7,
    evidence: list[str] | None = None,
    rationale: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    try:
        pr = propose_relation(
            _store(), src=src, relation=relation, target=target,
            confidence=confidence, evidence=evidence,
            rationale=rationale, session_id=session_id,
            dry_run=dry_run, proposed_by=_agent(),
        )
    except (ProposalError, ArtifactNotFoundError, ValueError) as e:
        raise ValueError(str(e)) from e
    return _proposal_response(pr, dry_run)


def _proposal_response(result, dry_run: bool) -> dict[str, Any]:
    pr = result.proposal if hasattr(result, "proposal") else result
    out: dict[str, Any] = {
        "proposal_id": pr.id,
        "status": pr.status.value,
        "kind": pr.kind.value,
        "dry_run": dry_run,
        "note": (
            "dry run — not written"
            if dry_run else "pending human approval via `vouch approve <id>`"
        ),
    }
    warnings = getattr(result, "warnings", None)
    if warnings:
        out["warnings"] = warnings
    return out


# === review-gate decisions (agents can approve on their own KBs if the
# host trusts them; in team setups, gate on the CLI side) =================


@mcp.tool()
def kb_approve(proposal_id: str, reason: str | None = None) -> dict[str, Any]:
    """Approve a proposal → durable artifact. Use carefully."""
    try:
        artifact = approve(_store(), proposal_id, approved_by=_agent(),
                           reason=reason)
    except (ArtifactNotFoundError, ValueError, ProposalError) as e:
        raise ValueError(str(e)) from e
    return {"kind": type(artifact).__name__.lower(), "id": artifact.id}


@mcp.tool()
def kb_reject(proposal_id: str, reason: str) -> dict[str, Any]:
    try:
        reject(_store(), proposal_id, rejected_by=_agent(), reason=reason)
    except (ArtifactNotFoundError, ValueError, ProposalError) as e:
        raise ValueError(str(e)) from e
    return {"proposal_id": proposal_id, "status": "rejected", "reason": reason}


@mcp.tool()
def kb_reject_extracted(
    page_id: str | None = None, reason: str | None = None,
) -> dict[str, Any]:
    """Mass-reject pending edges the auto-extractor filed (issue #224).

    Scope to one page's edges with `page_id`, or omit it to clear every
    pending auto-extracted edge across the KB.
    """
    try:
        rejected = reject_auto_extracted(
            _store(), rejected_by=_agent(), page_id=page_id,
            **({"reason": reason} if reason else {}),
        )
    except (ArtifactNotFoundError, ValueError, ProposalError) as e:
        raise ValueError(str(e)) from e
    return {"rejected": [p.id for p in rejected]}


@mcp.tool()
def kb_expire(apply: bool = False, days: int | None = None) -> dict[str, Any]:
    """Expire stale pending proposals (dry-run unless apply=True)."""
    try:
        result = expire_pending(
            _store(), apply=apply, expired_by=EXPIRE_ACTOR, days=days,
        )
    except (ArtifactNotFoundError, ValueError, ProposalError) as e:
        raise ValueError(str(e)) from e
    return {
        "threshold_days": result.threshold_days,
        "enabled": result.threshold_days > 0,
        "dry_run": not apply,
        "would_expire": [p.id for p in result.would_expire],
        "expired": [p.id for p in result.expired],
    }


# === lifecycle ============================================================


@mcp.tool()
def kb_supersede(old_claim_id: str, new_claim_id: str) -> dict[str, Any]:
    old, new = life.supersede(
        _store(), old_claim_id=old_claim_id, new_claim_id=new_claim_id,
        actor=_agent(),
    )
    return {"old": old.id, "new": new.id, "status": old.status.value}


@mcp.tool()
def kb_contradict(claim_a: str, claim_b: str) -> dict[str, Any]:
    a, b, rel = life.contradict(_store(), claim_a=claim_a, claim_b=claim_b,
                                actor=_agent())
    return {"a": a.id, "b": b.id, "relation_id": rel.id}


@mcp.tool()
def kb_archive(claim_id: str) -> dict[str, Any]:
    c = life.archive(_store(), claim_id=claim_id, actor=_agent())
    return {"id": c.id, "status": c.status.value}


@mcp.tool()
def kb_confirm(claim_id: str) -> dict[str, Any]:
    c = life.confirm(_store(), claim_id=claim_id, actor=_agent())
    return {"id": c.id, "last_confirmed_at": c.last_confirmed_at}


@mcp.tool()
def kb_cite(claim_id: str) -> list[dict[str, Any]]:
    """Return resolved citations (sources or evidence records) backing a claim."""
    out = []
    for c in life.cite(_store(), claim_id):
        if hasattr(c, "model_dump"):
            out.append(c.model_dump(mode="json"))
        else:
            out.append(c)
    return out


@mcp.tool()
def kb_source_verify() -> list[dict[str, Any]]:
    """Re-hash every source and report any drift."""
    results = verify_mod.verify_all(_store(), actor=_agent())
    return [
        {
            "source_id": r.source.id,
            "title": r.source.title,
            "stored_ok": r.stored_ok,
            "external_status": r.external_status,
            "note": r.note,
        }
        for r in results
    ]


# === sessions =============================================================


@mcp.tool()
async def kb_session_start(task: str | None = None, note: str | None = None) -> dict[str, Any]:
    store = _store()
    sess = sess_mod.session_start(store, agent=_agent(), task=task, note=note)
    ctx = mcp.get_context()
    if ctx.session is not None:
        volunteer_context.register_mcp_push(
            sess.id, ctx.session, asyncio.get_running_loop(),
        )
    return sess.model_dump(mode="json")


@mcp.tool()
def kb_volunteer_context(session_id: str, *, clear: bool = True) -> dict[str, Any]:
    """Poll confidence-gated context volunteered for an active session."""
    offers = volunteer_context.drain_pending(session_id, clear=clear)
    return {"volunteers": [o.to_dict() for o in offers]}


@mcp.tool()
def kb_session_end(session_id: str, note: str | None = None) -> dict[str, Any]:
    sess = sess_mod.session_end(_store(), session_id, note=note)
    return sess.model_dump(mode="json")


@mcp.tool()
def kb_crystallize(session_id: str, write_summary_page: bool = True
                   ) -> dict[str, Any]:
    """Approve every pending proposal in `session_id` (host must trust the agent)."""
    return sess_mod.crystallize(
        _store(), session_id, approver=_agent(),
        write_summary_page=write_summary_page,
    )


# === maintenance ==========================================================


@mcp.tool()
def kb_index_rebuild() -> dict[str, Any]:
    """Drop and rebuild state.db from the durable files."""
    return health.rebuild_index(_store())


@mcp.tool()
def kb_lint(stale_days: int = 180) -> dict[str, Any]:
    report = health.lint(_store(), stale_after_days=stale_days)
    return {
        "ok": report.ok,
        "findings": [
            {"severity": f.severity, "code": f.code,
             "message": f.message, "object_ids": f.object_ids}
            for f in report.findings
        ],
        "counts": report.counts,
    }


@mcp.tool()
def kb_doctor() -> dict[str, Any]:
    report = health.doctor(_store())
    return {
        "ok": report.ok,
        "findings": [
            {"severity": f.severity, "code": f.code,
             "message": f.message, "object_ids": f.object_ids}
            for f in report.findings
        ],
        "counts": report.counts,
    }


@mcp.tool()
def kb_export(out_path: str) -> dict[str, Any]:
    manifest = bundle.export(_store().kb_dir, dest=Path(out_path), actor=_agent())
    return {
        "bundle_id": manifest["bundle_id"],
        "files": len(manifest["files"]),
        "out": out_path,
    }


@mcp.tool()
def kb_export_check(bundle_path: str) -> dict[str, Any]:
    r = bundle.export_check(Path(bundle_path))
    return {
        "ok": r.ok, "bundle_id": r.bundle_id,
        "files_checked": r.files_checked, "issues": r.issues,
    }


@mcp.tool()
def kb_import_check(bundle_path: str) -> dict[str, Any]:
    r = bundle.import_check(_store().kb_dir, Path(bundle_path))
    return {
        "ok": r.ok, "bundle_id": r.bundle_id,
        "new_files": r.new_files, "conflicts": r.conflicts,
        "identical_files": len(r.identical), "issues": r.issues,
    }


@mcp.tool()
def kb_import_apply(bundle_path: str, on_conflict: str = "skip") -> dict[str, Any]:
    try:
        r = bundle.import_apply(
            _store().kb_dir, Path(bundle_path),
            on_conflict=on_conflict, actor=_agent(),
        )
    except (RuntimeError, ValueError) as e:
        raise ValueError(str(e)) from e
    health.rebuild_index(_store())
    return r


@mcp.tool()
def kb_audit(
    tail: int = 50,
    *,
    project: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Return the last N audit events, filtered to the viewer scope."""
    store = _store()
    viewer = viewer_from(
        config_path=store.config_path,
        project=project,
        agent=agent,
    )
    events = list(audit.read_events(store.kb_dir, store=store, viewer=viewer))[-tail:]
    return {
        "viewer": {"project": viewer.project, "agent": viewer.agent},
        "events": [e.model_dump(mode="json") for e in events],
    }


@mcp.tool()
def kb_reindex_embeddings(
    *, backfill: bool = False, force: bool = False, model: str | None = None,
) -> dict[str, Any]:
    """Re-encode every artifact under the current embedding adapter."""
    from .embeddings.migration import backfill_embeddings
    store = _store()
    if model:
        from .embeddings import get_embedder
        get_embedder(model)
    n = backfill_embeddings(store, force=force)
    return {"touched": n, "model": _current_model_name()}


@mcp.tool()
def kb_dedup_scan(
    *, threshold: float = 0.95, dry_run: bool = False,
) -> dict[str, Any]:
    """Find near-duplicate artifacts via embedding cosine."""
    from .embeddings.dedup import scan_all
    store = _store()
    rows = scan_all(store.kb_dir, threshold=threshold, dry_run=dry_run)
    return {"duplicates": rows, "threshold": threshold}


@mcp.tool()
def kb_eval_embeddings(*, queries_path: str, k: int = 10) -> dict[str, Any]:
    """Run retrieval eval over a JSONL queries file."""
    from pathlib import Path

    from .embeddings.scorer import evaluate
    store = _store()
    return evaluate(
        kb_dir=store.kb_dir,
        queries_file=Path(queries_path),
        k=k,
    )


@mcp.tool()
def kb_embeddings_stats() -> dict[str, Any]:
    """Model identity, per-kind counts, query cache stats."""
    from . import index_db
    from .embeddings.cache import query_cache_stats
    store = _store()
    meta = index_db.get_embedding_meta(store.kb_dir)
    counts: dict[str, int] = {}
    with index_db.open_db(store.kb_dir) as conn:
        for k, n in conn.execute(
            "SELECT kind, COUNT(*) FROM embedding_index GROUP BY kind"
        ):
            counts[k] = int(n)
    return {
        "model": meta.get("embedding_model"),
        "model_version": meta.get("embedding_model_version"),
        "dim": meta.get("embedding_dim"),
        "counts": counts,
        "query_cache": query_cache_stats(store.kb_dir),
    }


# === provenance (why / trace / impact / graph) ===========================


@mcp.tool()
def kb_why(claim_id: str, *, depth: int = 3) -> dict[str, Any]:
    """Backward provenance for a claim: cites, session, supersedes, approval.

    depth: how many hops of provenance to expand (default 3).
    """
    from . import provenance as prov
    return prov.why(_store(), claim_id=claim_id, depth=depth)


@mcp.tool()
def kb_trace(from_id: str, to_id: str) -> dict[str, Any]:
    """Shortest typed-edge path between two artifacts (or found=false)."""
    from . import provenance as prov
    return prov.trace(_store(), from_id=from_id, to_id=to_id)


@mcp.tool()
def kb_impact(
    claim_id: str, *, depth: int = 1, op: str | None = None
) -> dict[str, Any]:
    """Forward impact: dependents, and breakage if op (archive/contradict/supersede)."""
    from . import provenance as prov
    return prov.impact(_store(), claim_id=claim_id, depth=depth, op=op)


@mcp.tool()
def kb_graph_export(*, session: str | None = None, format: str = "dot") -> dict[str, Any]:
    """Render the provenance DAG (or one session's subgraph) as dot/mermaid."""
    from . import provenance as prov
    graph = prov.graph_export(_store(), session=session, fmt=format)
    return {"format": format, "graph": graph}


@mcp.tool()
def kb_provenance_rebuild() -> dict[str, Any]:
    """Rebuild the prov_edges cache from durable files; returns edge count."""
    from . import provenance as prov
    return {"edges": prov.rebuild_prov_edges(_store())}


# === cross-session themes =================================================


@mcp.tool()
def kb_detect_themes(
    *,
    min_sessions: int | None = None,
    min_claims: int | None = None,
    top_k: int | None = None,
) -> dict[str, Any]:
    """Detect recurring entity clusters across completed sessions.

    Read-only — returns ranked clusters without persisting anything.
    Scoring is deterministic (entity co-occurrence, no LLM).

    min_sessions: minimum sessions an entity pair must span (default from config or 2).
    min_claims: minimum claims supporting the cluster (default from config or 3).
    top_k: maximum clusters to return (default from config or 10).
    """
    from . import themes
    store = _store()
    result = themes.detect_themes(
        store,
        min_sessions=min_sessions,
        min_claims=min_claims,
        top_k=top_k,
    )
    return {
        "clusters": [
            {
                "entities": c.entities,
                "claim_ids": c.claim_ids,
                "session_ids": c.session_ids,
                "score": c.score,
                "session_count": c.session_count,
                "claim_count": c.claim_count,
            }
            for c in result.clusters
        ],
        "config": result.config_used,
    }


@mcp.tool()
def kb_propose_theme(
    *,
    entities: list[str],
    claim_ids: list[str],
    session_ids: list[str],
    score: float = 0.0,
    agent: str | None = None,
) -> dict[str, Any]:
    """Propose a theme synthesis page from a detected cluster.

    Routes through the review gate — appears in kb.list_pending.
    Pass a cluster from kb.detect_themes directly.
    """
    from . import themes
    store = _store()
    actor = agent or os.environ.get("VOUCH_AGENT", "unknown-agent")
    cluster = themes.ThemeCluster(
        entities=entities,
        claim_ids=claim_ids,
        session_ids=session_ids,
        score=score,
        session_count=len(session_ids),
        claim_count=len(claim_ids),
    )
    return themes.propose_theme(store, cluster, proposed_by=actor)


def _current_model_name() -> str:
    try:
        from .embeddings import get_embedder
        return get_embedder().name
    except Exception:
        return ""


trust_mod.install_mcp_trust_wrappers(mcp)


def run_stdio() -> None:
    """Entry point used by `vouch serve`."""
    configure_logging()
    trust_mod.set_stdio_default(trust_mod.MCP_STDIO)
    mcp.run()
