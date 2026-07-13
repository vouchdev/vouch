"""Newline-delimited JSON tool server — AKBP-style transport.

Reads request envelopes one per line from stdin, writes response envelopes
one per line to stdout. Same surface as the MCP server, different wire
format. Useful when MCP isn't available (older clients, embedded harnesses)
or when you want the server in a plain pipe.

Request envelope:
  {"id": "req-1", "method": "kb.search", "params": {"query": "jwt", "limit": 5}}

Response envelope (success):
  {"id": "req-1", "ok": true, "result": {...}}

Response envelope (failure):
  {"id": "req-1", "ok": false, "error": {"code": "...", "message": "..."}}
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml

from . import audit, bundle, health, volunteer_context
from . import compile as compile_mod
from . import digest as digest_mod
from . import lifecycle as life
from . import metrics as metrics_mod
from . import salience as salience_mod
from . import sessions as sess_mod
from . import skills as skills_mod
from . import trust as trust_mod
from . import verify as verify_mod
from .capabilities import capabilities as build_caps
from .context import build_context_pack
from .logging_config import configure_logging
from .models import ProposalStatus
from .page_filters import filter_pages
from .proposals import (
    EXPIRE_ACTOR,
    ProposalError,
    approve,
    expire_pending,
    propose_claim,
    propose_delete,
    propose_entity,
    propose_page,
    propose_relation,
    reject,
    reject_auto_extracted,
)
from .stats import collect_activity, collect_stats
from .storage import (
    ArtifactNotFoundError,
    KBNotFoundError,
    KBStore,
    discover_root,
)
from .synthesize import synthesize

_log = logging.getLogger("vouch.jsonl_server")

# Per-request actor override. The HTTP transport sets this from the
# X-Vouch-Agent header so audit attribution is correct without mutating
# process-wide env (each ThreadingHTTPServer request thread gets its own
# context, so this is concurrency-safe). stdio/JSONL leave it unset and
# fall back to VOUCH_AGENT.
_actor: ContextVar[str | None] = ContextVar("vouch_actor", default=None)


def _store() -> KBStore:
    try:
        return KBStore(discover_root())
    except KBNotFoundError as e:
        raise RuntimeError(str(e)) from e


def _agent() -> str:
    return _actor.get() or os.environ.get("VOUCH_AGENT", "unknown-agent")


# --- per-method handlers ---------------------------------------------------


def _h_capabilities(_: dict) -> dict:
    try:
        publish_skills = skills_mod.publish_skills_enabled(_store())
    except Exception:
        publish_skills = True
    return build_caps(publish_skills=publish_skills).model_dump(mode="json")


def _h_status(_: dict) -> dict:
    return health.status(_store())


def _h_stats(p: dict) -> dict:
    days = int(p.get("days", 30))
    since = None if days == 0 else days
    return collect_stats(_store(), since_days=since)


def _h_activity(p: dict) -> dict:
    from .scoping import viewer_from_params

    s = _store()
    viewer = viewer_from_params(s, p)
    return collect_activity(
        s,
        days=int(p.get("days", 365)),
        tz_offset_minutes=int(p.get("tz_offset_minutes", 0)),
        tz=p.get("tz"),
        viewer=viewer,
    )


def _h_digest(p: dict) -> dict:
    d = digest_mod.build(
        _store(),
        since=metrics_mod.parse_since(str(p.get("since", digest_mod.DEFAULT_SINCE_SPEC))),
        stale_after_days=int(p.get("stale_days", metrics_mod.DEFAULT_STALE_DAYS)),
        limit=int(p.get("limit", digest_mod.DEFAULT_LIMIT)),
    )
    return d.to_dict()


def _h_search(p: dict) -> dict:
    from .context import search_kb

    # One shared implementation across MCP / JSONL — see context.search_kb.
    # No explicit backend on the wire -> the deployment's retrieval.backend
    # decides, same as kb.context.
    return search_kb(
        _store(),
        query=p["query"],
        limit=int(p.get("limit", 10)),
        backend=p.get("backend"),
        min_score=float(p.get("min_score", 0.0)),
        project=p.get("project"),
        agent=p.get("agent"),
    )


def _load_cfg(store: KBStore) -> dict:
    try:
        loaded = yaml.safe_load((store.kb_dir / "config.yaml").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _h_experts(p: dict) -> dict:
    from .experts import rank_experts

    return {
        "experts": rank_experts(
            _store(),
            p["topic"],
            limit=int(p.get("limit", 10)),
            min_claims=int(p.get("min_claims", 1)),
            weight=p.get("weight", "count"),
        )
    }


def _h_neighbors(p: dict) -> dict:
    from .graph import find_neighbors

    return find_neighbors(
        _store(),
        p["node_id"],
        depth=int(p.get("depth", 1)),
        rel_types=p.get("rel_types"),
        max_nodes=int(p.get("max_nodes", 50)),
    )


def _h_context(p: dict) -> dict:
    store = _store()
    query = p["task"]
    cfg = _load_cfg(store)
    session_id = p.get("session_id")
    if session_id:
        _, window, _ = salience_mod.reflex_cfg(cfg)
        salience_mod.record_query(session_id, query, window=window)
    result: dict = build_context_pack(  # type: ignore[assignment]
        store,
        query=query,
        limit=int(p.get("limit", 10)),
        max_chars=int(p["max_chars"]) if p.get("max_chars") is not None else None,
        min_items=int(p.get("min_items", 0)),
        require_citations=bool(p.get("require_citations", False)),
        fail_on_warnings=bool(p.get("fail_on_warnings", False)),
        fail_on_budget_truncation=bool(p.get("fail_on_budget_truncation", False)),
        project=p.get("project"),
        agent=p.get("agent"),
        expand_graph=bool(p.get("expand_graph", False)),
        graph_depth=int(p.get("graph_depth", 1)),
        graph_limit=int(p.get("graph_limit", 20)),
        graph_rel_types=p.get("graph_rel_types"),
    )
    return salience_mod.attach_salience(result, store, session_id, cfg)


def _h_synthesize(p: dict) -> dict:
    return synthesize(
        _store(),
        query=p["query"],
        depth=int(p.get("depth", 3)),
        max_chars=int(p.get("max_chars", 4000)),
        llm=bool(p.get("llm", False)),
    )


def _h_read_page(p: dict) -> dict:
    return _store().get_page(p["page_id"]).model_dump(mode="json")


def _h_read_claim(p: dict) -> dict:
    return _store().get_claim(p["claim_id"]).model_dump(mode="json")


def _h_read_entity(p: dict) -> dict:
    return _store().get_entity(p["entity_id"]).model_dump(mode="json")


def _h_read_relation(p: dict) -> dict:
    return _store().get_relation(p["relation_id"]).model_dump(mode="json")


def _h_diff(p: dict) -> dict:
    from dataclasses import asdict

    from .diff import diff_artifacts

    return asdict(diff_artifacts(_store(), p["old_id"], p.get("new_id")))


def _h_list_pages(p: dict) -> list[dict]:
    pages = filter_pages(
        _store().list_pages(),
        kind=p.get("type"),
        equals=p.get("meta"),
        before=p.get("meta_before"),
        after=p.get("meta_after"),
    )
    return [pg.model_dump(mode="json") for pg in pages]


def _h_list_claims(p: dict) -> list[dict]:
    cs = _store().list_claims()
    if p.get("status"):
        cs = [c for c in cs if c.status.value == p["status"]]
    return [c.model_dump(mode="json") for c in cs]


def _h_list_entities(p: dict) -> list[dict]:
    es = _store().list_entities()
    if p.get("entity_type"):
        es = [e for e in es if e.type.value == p["entity_type"]]
    return [e.model_dump(mode="json") for e in es]


def _h_list_relations(p: dict) -> list[dict]:
    s = _store()
    rels = s.list_relations()
    node = p.get("node_id")
    if node:
        rels = [r for r in rels if r.source == node or r.target == node]
    return [r.model_dump(mode="json") for r in rels]


def _h_list_sources(_: dict) -> list[dict]:
    return [s.model_dump(mode="json") for s in _store().list_sources()]


def _h_list_pending(_: dict) -> list[dict]:
    return [
        p.model_dump(mode="json")
        for p in _store().list_proposals(ProposalStatus.PENDING)
    ]


def _h_triage_pending(p: dict) -> list[dict]:
    from . import triage as triage_mod

    return triage_mod.triage_pending(_store(), proposal_ids=p.get("proposal_ids"))


def _h_register_source(p: dict) -> dict:
    s = _store()
    src = s.put_source(
        p["content"].encode("utf-8"),
        title=p.get("title"),
        url=p.get("url"),
        locator=p.get("url") or p.get("title") or "inline",
        source_type=p.get("source_type", "file"),
        media_type=p.get("media_type", "text/plain"),
    )
    audit.log_event(s.kb_dir, event="source.add", actor=_agent(), object_ids=[src.id])
    return src.model_dump(mode="json")


def _h_register_source_from_path(p: dict) -> dict:
    s = _store()
    path, body = s.read_under_root(p["path"])
    src = s.put_source(
        body,
        title=p.get("title") or path.name,
        url=p.get("url"),
        locator=str(path),
        source_type=p.get("source_type", "file"),
    )
    audit.log_event(s.kb_dir, event="source.add", actor=_agent(), object_ids=[src.id])
    return src.model_dump(mode="json")


def _h_propose_claim(p: dict) -> dict:
    result = propose_claim(
        _store(),
        text=p["text"],
        evidence=list(p["evidence"]),
        claim_type=p.get("claim_type", "observation"),
        confidence=float(p.get("confidence", 0.7)),
        entities=p.get("entities"),
        tags=p.get("tags"),
        rationale=p.get("rationale"),
        slug_hint=p.get("slug_hint"),
        session_id=p.get("session_id"),
        dry_run=bool(p.get("dry_run", False)),
        proposed_by=_agent(),
    )
    pr = result.proposal
    out: dict = {
        "proposal_id": pr.id,
        "status": pr.status.value,
        "kind": pr.kind.value,
        "dry_run": bool(p.get("dry_run", False)),
    }
    if result.warnings:
        out["warnings"] = result.warnings
    return out


def _h_propose_page(p: dict) -> dict:
    pr = propose_page(
        _store(),
        title=p["title"], body=p.get("body", ""),
        page_type=p.get("page_type", "concept"),
        claim_ids=p.get("claim_ids"),
        entity_ids=p.get("entity_ids"),
        source_ids=p.get("source_ids"),
        metadata=p.get("metadata"),
        rationale=p.get("rationale"),
        slug_hint=p.get("slug_hint"),
        session_id=p.get("session_id"),
        dry_run=bool(p.get("dry_run", False)),
        proposed_by=_agent(),
    )
    return {"proposal_id": pr.id, "status": pr.status.value, "kind": pr.kind.value}


def _h_compile(p: dict) -> dict:
    try:
        report = compile_mod.compile_kb(
            _store(), triggered_by=_agent(),
            max_pages=p.get("max_pages"),
            dry_run=bool(p.get("dry_run", False)),
            session_id=p.get("session_id"),
        )
    except compile_mod.CompileError as e:
        # config/LLM/output-shape failures are caller-visible conditions,
        # not server faults — surface them on the ValueError path so the
        # envelope carries a clean message instead of internal_error.
        raise ValueError(str(e)) from e
    return report.to_dict()


def _h_summarize_session(p: dict) -> dict:
    from . import session_split
    return session_split.summarize(
        _store(), p["session_id"], mode=p.get("mode", "auto"),
    )


def _h_list_sessions(p: dict) -> dict:
    from . import session_split
    return {"sessions": session_split.build_session_rows(_store())}


def _h_session_transcript(p: dict) -> dict:
    from . import transcript
    session_id = p["session_id"]
    agent = p.get("agent")
    if agent is not None and agent not in ("claude", "codex"):
        raise ValueError(f"unknown agent: {agent!r} (expected 'claude' or 'codex')")
    return transcript.load_transcript(_store(), session_id, agent=agent)


def _h_propose_entity(p: dict) -> dict:
    pr = propose_entity(
        _store(),
        name=p["name"], entity_type=p["entity_type"],
        aliases=p.get("aliases"), description=p.get("description"),
        rationale=p.get("rationale"), slug_hint=p.get("slug_hint"),
        session_id=p.get("session_id"),
        dry_run=bool(p.get("dry_run", False)),
        proposed_by=_agent(),
    )
    return {"proposal_id": pr.id, "status": pr.status.value, "kind": pr.kind.value}


def _h_propose_relation(p: dict) -> dict:
    pr = propose_relation(
        _store(),
        src=p["src"], relation=p["relation"], target=p["target"],
        confidence=float(p.get("confidence", 0.7)),
        evidence=p.get("evidence"),
        rationale=p.get("rationale"),
        session_id=p.get("session_id"),
        dry_run=bool(p.get("dry_run", False)),
        proposed_by=_agent(),
    )
    return {"proposal_id": pr.id, "status": pr.status.value, "kind": pr.kind.value}


def _h_propose_delete(p: dict) -> dict:
    pr = propose_delete(
        _store(),
        target_kind=p["target_kind"],
        target_id=p["target_id"],
        rationale=p.get("rationale"),
        session_id=p.get("session_id"),
        dry_run=bool(p.get("dry_run", False)),
        proposed_by=_agent(),
    )
    return {
        "proposal_id": pr.id,
        "status": pr.status.value,
        "kind": pr.kind.value,
        "dry_run": bool(p.get("dry_run", False)),
    }


def _h_approve(p: dict) -> dict:
    a = approve(_store(), p["proposal_id"], approved_by=_agent(),
                reason=p.get("reason"))
    return {"kind": type(a).__name__.lower(), "id": a.id}


def _h_reject(p: dict) -> dict:
    reject(_store(), p["proposal_id"], rejected_by=_agent(), reason=p["reason"])
    return {"proposal_id": p["proposal_id"], "status": "rejected"}


def _h_reject_extracted(p: dict) -> dict:
    kwargs: dict[str, Any] = {"page_id": p.get("page_id")}
    if p.get("reason"):
        kwargs["reason"] = p["reason"]
    rejected = reject_auto_extracted(_store(), rejected_by=_agent(), **kwargs)
    return {"rejected": [pr.id for pr in rejected]}


def _h_expire(p: dict) -> dict:
    result = expire_pending(
        _store(),
        apply=bool(p.get("apply")),
        expired_by=EXPIRE_ACTOR,
        days=p.get("days"),
    )
    return {
        "threshold_days": result.threshold_days,
        "enabled": result.threshold_days > 0,
        "dry_run": not bool(p.get("apply")),
        "would_expire": [pr.id for pr in result.would_expire],
        "expired": [pr.id for pr in result.expired],
    }


def _h_supersede(p: dict) -> dict:
    old, new = life.supersede(
        _store(), old_claim_id=p["old_claim_id"],
        new_claim_id=p["new_claim_id"], actor=_agent(),
    )
    return {"old": old.id, "new": new.id, "status": old.status.value}


def _h_contradict(p: dict) -> dict:
    a, b, rel = life.contradict(_store(), claim_a=p["claim_a"],
                                claim_b=p["claim_b"], actor=_agent())
    return {"a": a.id, "b": b.id, "relation_id": rel.id}


def _h_archive(p: dict) -> dict:
    c = life.archive(_store(), claim_id=p["claim_id"], actor=_agent())
    return {"id": c.id, "status": c.status.value}


def _h_confirm(p: dict) -> dict:
    c = life.confirm(_store(), claim_id=p["claim_id"], actor=_agent())
    return {"id": c.id, "last_confirmed_at": c.last_confirmed_at.isoformat()
            if c.last_confirmed_at else None}


def _h_clear_claims(p: dict) -> dict:
    from datetime import datetime
    before_dt = None
    if p.get("before"):
        before_dt = datetime.fromisoformat(p["before"])
    to_clear = life.clear_claims(
        _store(),
        auto_only=p.get("auto_only", True),
        before=before_dt,
        actor=_agent(),
        dry_run=p.get("dry_run", False),
    )
    return {
        "count": len(to_clear),
        "claim_ids": [c.id for c in to_clear],
        "dry_run": p.get("dry_run", False),
    }


def _h_cite(p: dict) -> list:
    out = []
    for c in life.cite(_store(), p["claim_id"]):
        out.append(c.model_dump(mode="json") if hasattr(c, "model_dump") else c)
    return out


def _h_source_verify(_: dict) -> list[dict]:
    results = verify_mod.verify_all(_store(), actor=_agent())
    return [
        {"source_id": r.source.id, "title": r.source.title,
         "stored_ok": r.stored_ok, "external_status": r.external_status,
         "note": r.note}
        for r in results
    ]


def _h_session_start(p: dict) -> dict:
    return sess_mod.session_start(
        _store(), agent=_agent(), task=p.get("task"), note=p.get("note"),
    ).model_dump(mode="json")


def _h_session_end(p: dict) -> dict:
    return sess_mod.session_end(_store(), p["session_id"],
                                note=p.get("note")).model_dump(mode="json")


def _h_crystallize(p: dict) -> dict:
    return sess_mod.crystallize(
        _store(), p["session_id"], approver=_agent(),
        write_summary_page=bool(p.get("write_summary_page", True)),
    )


def _h_volunteer_context(p: dict) -> dict:
    offers = volunteer_context.drain_pending(
        p["session_id"],
        clear=bool(p.get("clear", True)),
    )
    return {"volunteers": [o.to_dict() for o in offers]}


def _h_index_rebuild(_: dict) -> dict:
    return health.rebuild_index(_store())


def _h_lint(p: dict) -> dict:
    report = health.lint(_store(),
                         stale_after_days=int(p.get("stale_days", 180)))
    return {
        "ok": report.ok,
        "findings": [
            {"severity": f.severity, "code": f.code,
             "message": f.message, "object_ids": f.object_ids}
            for f in report.findings
        ],
        "counts": report.counts,
    }


def _h_doctor(_: dict) -> dict:
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


def _h_export(p: dict) -> dict:
    manifest = bundle.export(_store().kb_dir, dest=Path(p["out_path"]),
                             actor=_agent())
    return {"bundle_id": manifest["bundle_id"],
            "files": len(manifest["files"]), "out": p["out_path"]}


def _h_export_check(p: dict) -> dict:
    r = bundle.export_check(Path(p["bundle_path"]))
    return {"ok": r.ok, "bundle_id": r.bundle_id,
            "files_checked": r.files_checked, "issues": r.issues}


def _h_import_check(p: dict) -> dict:
    r = bundle.import_check(_store().kb_dir, Path(p["bundle_path"]))
    return {"ok": r.ok, "bundle_id": r.bundle_id,
            "new_files": r.new_files, "conflicts": r.conflicts,
            "identical_files": len(r.identical), "issues": r.issues}


def _h_import_apply(p: dict) -> dict:
    r = bundle.import_apply(
        _store().kb_dir, Path(p["bundle_path"]),
        on_conflict=p.get("on_conflict", "skip"), actor=_agent(),
    )
    health.rebuild_index(_store())
    return r


def _h_audit(p: dict) -> dict:
    from .scoping import viewer_from_params

    s = _store()
    viewer = viewer_from_params(s, p)
    tail = int(p.get("tail", 50))
    events = list(audit.read_events(s.kb_dir, store=s, viewer=viewer))[-tail:]
    return {
        "viewer": {"project": viewer.project, "agent": viewer.agent},
        "events": [e.model_dump(mode="json") for e in events],
    }


def _h_reindex_embeddings(p: dict) -> dict:
    from .embeddings.migration import backfill_embeddings
    n = backfill_embeddings(_store(), force=bool(p.get("force", False)))
    return {"touched": n}


def _h_dedup_scan(p: dict) -> dict:
    from .embeddings.dedup import scan_all
    return {
        "duplicates": scan_all(
            _store().kb_dir,
            threshold=float(p.get("threshold", 0.95)),
            dry_run=bool(p.get("dry_run", False)),
        ),
    }


def _h_eval_embeddings(p: dict) -> dict:
    from pathlib import Path

    from .embeddings.scorer import evaluate
    return evaluate(
        kb_dir=_store().kb_dir,
        queries_file=Path(p["queries_path"]),
        k=int(p.get("k", 10)),
    )


def _h_embeddings_stats(_: dict) -> dict:
    from . import index_db
    from .embeddings.cache import query_cache_stats
    store = _store()
    meta = index_db.get_embedding_meta(store.kb_dir)
    with index_db.open_db(store.kb_dir) as conn:
        counts = {
            k: int(n) for k, n in conn.execute(
                "SELECT kind, COUNT(*) FROM embedding_index GROUP BY kind"
            )
        }
    return {
        "model": meta.get("embedding_model"),
        "counts": counts,
        "query_cache": query_cache_stats(store.kb_dir),
    }


def _h_list_skills(_: dict) -> list[dict]:
    return skills_mod.list_skills(_store())


def _h_get_skill(p: dict) -> dict:
    name = p.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("`name` is required")
    try:
        return skills_mod.get_skill(_store(), name)
    except KeyError as e:
        raise ValueError(str(e)) from e


def _h_why(p: dict) -> dict:
    from . import provenance as prov

    return prov.why(_store(), claim_id=p["claim_id"], depth=int(p.get("depth", 3)))


def _h_trace(p: dict) -> dict:
    from . import provenance as prov

    return prov.trace(_store(), from_id=p["from"], to_id=p["to"])


def _h_impact(p: dict) -> dict:
    from . import provenance as prov

    return prov.impact(
        _store(),
        claim_id=p["claim_id"],
        depth=int(p.get("depth", 1)),
        op=p.get("op"),
    )


def _h_graph_export(p: dict) -> dict:
    from . import provenance as prov

    fmt = p.get("format", "dot")
    graph = prov.graph_export(_store(), session=p.get("session"), fmt=fmt)
    return {"format": fmt, "graph": graph}


def _h_provenance_rebuild(_: dict) -> dict:
    from . import provenance as prov

    return {"edges": prov.rebuild_prov_edges(_store())}


def _h_detect_themes(p: dict) -> dict:
    from . import themes

    result = themes.detect_themes(
        _store(),
        min_sessions=p.get("min_sessions"),
        min_claims=p.get("min_claims"),
        top_k=p.get("top_k"),
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


def _h_propose_theme(p: dict) -> dict:
    from . import themes

    store = _store()
    actor = p.get("agent") or os.environ.get("VOUCH_AGENT", "unknown-agent")
    cluster = themes.ThemeCluster(
        entities=p["entities"],
        claim_ids=p["claim_ids"],
        session_ids=p.get("session_ids", []),
        score=float(p.get("score", 0.0)),
        session_count=len(p.get("session_ids", [])),
        claim_count=len(p["claim_ids"]),
    )
    return themes.propose_theme(store, cluster, proposed_by=actor)


HANDLERS: dict[str, Callable[[dict], Any]] = {
    "kb.capabilities": _h_capabilities,
    "kb.status": _h_status,
    "kb.stats": _h_stats,
    "kb.activity": _h_activity,
    "kb.digest": _h_digest,
    "kb.search": _h_search,
    "kb.neighbors": _h_neighbors,
    "kb.experts": _h_experts,
    "kb.context": _h_context,
    "kb.synthesize": _h_synthesize,
    "kb.read_page": _h_read_page,
    "kb.read_claim": _h_read_claim,
    "kb.read_entity": _h_read_entity,
    "kb.read_relation": _h_read_relation,
    "kb.diff": _h_diff,
    "kb.list_pages": _h_list_pages,
    "kb.list_claims": _h_list_claims,
    "kb.list_entities": _h_list_entities,
    "kb.list_relations": _h_list_relations,
    "kb.list_sources": _h_list_sources,
    "kb.list_pending": _h_list_pending,
    "kb.triage_pending": _h_triage_pending,
    "kb.register_source": _h_register_source,
    "kb.register_source_from_path": _h_register_source_from_path,
    "kb.propose_claim": _h_propose_claim,
    "kb.propose_page": _h_propose_page,
    "kb.compile": _h_compile,
    "kb.summarize_session": _h_summarize_session,
    "kb.list_sessions": _h_list_sessions,
    "kb.session_transcript": _h_session_transcript,
    "kb.propose_entity": _h_propose_entity,
    "kb.propose_relation": _h_propose_relation,
    "kb.propose_delete": _h_propose_delete,
    "kb.approve": _h_approve,
    "kb.reject": _h_reject,
    "kb.reject_extracted": _h_reject_extracted,
    "kb.expire": _h_expire,
    "kb.supersede": _h_supersede,
    "kb.contradict": _h_contradict,
    "kb.archive": _h_archive,
    "kb.confirm": _h_confirm,
    "kb.clear_claims": _h_clear_claims,
    "kb.cite": _h_cite,
    "kb.source_verify": _h_source_verify,
    "kb.session_start": _h_session_start,
    "kb.session_end": _h_session_end,
    "kb.volunteer_context": _h_volunteer_context,
    "kb.crystallize": _h_crystallize,
    "kb.index_rebuild": _h_index_rebuild,
    "kb.lint": _h_lint,
    "kb.doctor": _h_doctor,
    "kb.export": _h_export,
    "kb.export_check": _h_export_check,
    "kb.import_check": _h_import_check,
    "kb.import_apply": _h_import_apply,
    "kb.audit": _h_audit,
    "kb.reindex_embeddings": _h_reindex_embeddings,
    "kb.dedup_scan": _h_dedup_scan,
    "kb.eval_embeddings": _h_eval_embeddings,
    "kb.embeddings_stats": _h_embeddings_stats,
    "kb.why": _h_why,
    "kb.trace": _h_trace,
    "kb.impact": _h_impact,
    "kb.graph_export": _h_graph_export,
    "kb.provenance_rebuild": _h_provenance_rebuild,
    "kb.detect_themes": _h_detect_themes,
    "kb.propose_theme": _h_propose_theme,
    "kb.list_skills": _h_list_skills,
    "kb.get_skill": _h_get_skill,
}


def handle_request(envelope: dict) -> dict:
    """Pure function — no I/O. Useful for tests."""
    req_id = envelope.get("id")
    method = envelope.get("method")
    params = envelope.get("params") or {}
    if not method or method not in HANDLERS:
        return {
            "id": req_id, "ok": False,
            "error": {"code": "method_not_found", "message": f"unknown method: {method}"},
        }
    try:
        result = HANDLERS[method](params)
        return {
            "id": req_id,
            "ok": True,
            "result": trust_mod.finish_kb_result(result),
        }
    except skills_mod.SkillsDisabledError as e:
        return {
            "id": req_id, "ok": False,
            "error": {"code": "permission_denied", "message": str(e)},
        }
    except KeyError as e:
        return {
            "id": req_id, "ok": False,
            "error": {"code": "missing_param", "message": str(e)},
        }
    except (ValueError, ProposalError, ArtifactNotFoundError) as e:
        return {
            "id": req_id, "ok": False,
            "error": {"code": "invalid_request", "message": str(e)},
        }
    except Exception as e:
        _log.exception("internal error handling %s", method)
        return {
            "id": req_id, "ok": False,
            "error": {
                "code": "internal_error",
                "message": str(e),
            },
        }


def run_jsonl(stdin=None, stdout=None) -> None:
    """Read one request per line, write one response per line."""
    configure_logging()
    trust_mod.set_stdio_default(trust_mod.JSONL_STDIO)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as e:
            stdout.write(json.dumps({
                "id": None, "ok": False,
                "error": {"code": "invalid_json", "message": str(e)},
            }) + "\n")
            stdout.flush()
            continue
        response = handle_request(envelope)
        stdout.write(json.dumps(response, default=str) + "\n")
        stdout.flush()
