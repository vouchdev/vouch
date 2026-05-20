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
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import audit, bundle, health
from . import lifecycle as life
from . import sessions as sess_mod
from . import verify as verify_mod
from .capabilities import capabilities as build_caps
from .context import build_context_pack
from .models import ProposalStatus
from .proposals import (
    ProposalError,
    approve,
    propose_claim,
    propose_entity,
    propose_page,
    propose_relation,
    reject,
)
from .storage import (
    ArtifactNotFoundError,
    KBNotFoundError,
    KBStore,
    discover_root,
)


def _store() -> KBStore:
    try:
        return KBStore(discover_root())
    except KBNotFoundError as e:
        raise RuntimeError(str(e)) from e


def _agent() -> str:
    return os.environ.get("VOUCH_AGENT", "unknown-agent")


# --- per-method handlers ---------------------------------------------------


def _h_capabilities(_: dict) -> dict:
    return build_caps().model_dump(mode="json")


def _h_status(_: dict) -> dict:
    return health.status(_store())


def _h_search(p: dict) -> list[dict]:
    from . import index_db
    s = _store()
    q = p["query"]
    limit = int(p.get("limit", 10))
    backend_arg = p.get("backend", "auto")
    min_score = float(p.get("min_score", 0.0))
    hits: list[tuple[str, str, str, float]] = []
    used = backend_arg

    valid_backends = {"auto", "embedding", "fts5", "substring", "hybrid"}
    if backend_arg not in valid_backends:
        raise ValueError(
            f"unknown backend: {backend_arg!r} "
            f"(expected one of {sorted(valid_backends)})"
        )

    if backend_arg in ("auto", "embedding"):
        hits = index_db.search_semantic(
            s.kb_dir, q, limit=limit, min_score=min_score,
        )
        if hits:
            used = "embedding"
    if not hits and backend_arg in ("auto", "fts5"):
        try:
            hits = index_db.search(s.kb_dir, q, limit=limit)
            used = "fts5" if hits else used
        except Exception:
            hits = []
    if not hits and backend_arg in ("auto", "substring"):
        hits = s.search_substring(q, limit=limit)
        used = "substring"
    if backend_arg == "hybrid":
        from .embeddings.fusion import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
            rrf_fuse,
        )
        # Hybrid must honour min_score and survive FTS failures the same
        # way the dedicated fts5 branch does.
        emb = index_db.search_semantic(
            s.kb_dir, q, limit=limit * 2, min_score=min_score,
        )
        try:
            fts = index_db.search(s.kb_dir, q, limit=limit * 2)
        except Exception:
            fts = []
        hits = rrf_fuse(emb, fts, limit=limit)
        used = "hybrid"

    return [
        {"kind": k, "id": i, "snippet": sn, "score": sc, "backend": used}
        for k, i, sn, sc in hits
    ]



def _h_context(p: dict) -> dict:
    return build_context_pack(  # type: ignore[return-value]
        _store(),
        query=p["task"],
        limit=int(p.get("limit", 10)),
        max_chars=p.get("max_chars"),
        min_items=int(p.get("min_items", 0)),
        require_citations=bool(p.get("require_citations", False)),
        fail_on_warnings=bool(p.get("fail_on_warnings", False)),
        fail_on_budget_truncation=bool(p.get("fail_on_budget_truncation", False)),
    )


def _h_read_page(p: dict) -> dict:
    return _store().get_page(p["page_id"]).model_dump(mode="json")


def _h_read_claim(p: dict) -> dict:
    return _store().get_claim(p["claim_id"]).model_dump(mode="json")


def _h_read_entity(p: dict) -> dict:
    return _store().get_entity(p["entity_id"]).model_dump(mode="json")


def _h_read_relation(p: dict) -> dict:
    return _store().get_relation(p["relation_id"]).model_dump(mode="json")


def _h_list_pages(_: dict) -> list[dict]:
    return [p.model_dump(mode="json") for p in _store().list_pages()]


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
    pr = propose_claim(
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
    return {"proposal_id": pr.id, "status": pr.status.value, "kind": pr.kind.value,
            "dry_run": bool(p.get("dry_run", False))}


def _h_propose_page(p: dict) -> dict:
    pr = propose_page(
        _store(),
        title=p["title"], body=p.get("body", ""),
        page_type=p.get("page_type", "concept"),
        claim_ids=p.get("claim_ids"),
        entity_ids=p.get("entity_ids"),
        source_ids=p.get("source_ids"),
        rationale=p.get("rationale"),
        slug_hint=p.get("slug_hint"),
        session_id=p.get("session_id"),
        dry_run=bool(p.get("dry_run", False)),
        proposed_by=_agent(),
    )
    return {"proposal_id": pr.id, "status": pr.status.value, "kind": pr.kind.value}


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


def _h_approve(p: dict) -> dict:
    a = approve(_store(), p["proposal_id"], approved_by=_agent(),
                reason=p.get("reason"))
    return {"kind": type(a).__name__.lower(), "id": a.id}


def _h_reject(p: dict) -> dict:
    reject(_store(), p["proposal_id"], rejected_by=_agent(), reason=p["reason"])
    return {"proposal_id": p["proposal_id"], "status": "rejected"}


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


def _h_audit(p: dict) -> list[dict]:
    return [
        e.model_dump(mode="json")
        for e in list(audit.read_events(_store().kb_dir))[-int(p.get("tail", 50)):]
    ]


HANDLERS: dict[str, Callable[[dict], Any]] = {
    "kb.capabilities": _h_capabilities,
    "kb.status": _h_status,
    "kb.search": _h_search,
    "kb.context": _h_context,
    "kb.read_page": _h_read_page,
    "kb.read_claim": _h_read_claim,
    "kb.read_entity": _h_read_entity,
    "kb.read_relation": _h_read_relation,
    "kb.list_pages": _h_list_pages,
    "kb.list_claims": _h_list_claims,
    "kb.list_entities": _h_list_entities,
    "kb.list_relations": _h_list_relations,
    "kb.list_sources": _h_list_sources,
    "kb.list_pending": _h_list_pending,
    "kb.register_source": _h_register_source,
    "kb.register_source_from_path": _h_register_source_from_path,
    "kb.propose_claim": _h_propose_claim,
    "kb.propose_page": _h_propose_page,
    "kb.propose_entity": _h_propose_entity,
    "kb.propose_relation": _h_propose_relation,
    "kb.approve": _h_approve,
    "kb.reject": _h_reject,
    "kb.supersede": _h_supersede,
    "kb.contradict": _h_contradict,
    "kb.archive": _h_archive,
    "kb.confirm": _h_confirm,
    "kb.cite": _h_cite,
    "kb.source_verify": _h_source_verify,
    "kb.session_start": _h_session_start,
    "kb.session_end": _h_session_end,
    "kb.crystallize": _h_crystallize,
    "kb.index_rebuild": _h_index_rebuild,
    "kb.lint": _h_lint,
    "kb.doctor": _h_doctor,
    "kb.export": _h_export,
    "kb.export_check": _h_export_check,
    "kb.import_check": _h_import_check,
    "kb.import_apply": _h_import_apply,
    "kb.audit": _h_audit,
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
        return {"id": req_id, "ok": True, "result": result}
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
        return {
            "id": req_id, "ok": False,
            "error": {
                "code": "internal_error",
                "message": str(e),
                "traceback": traceback.format_exc(),
            },
        }


def run_jsonl(stdin=None, stdout=None) -> None:
    """Read one request per line, write one response per line."""
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
