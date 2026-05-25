"""Health checks — `vouch doctor`, `vouch lint`, `vouch status`.

Doctor runs the full sweep (slow, comprehensive). Lint is the subset that
finds *user-actionable* problems: orphan claims, missing citations,
contradictions, stale claims. Status is a one-line summary used by tooling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import index_db
from .audit import count_events
from .models import ClaimStatus, ProposalStatus
from .storage import KBStore, sha256_hex
from .verify import verify_all


@dataclass
class Finding:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    object_ids: list[str] = field(default_factory=list)


@dataclass
class HealthReport:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def status(store: KBStore) -> dict:
    """Quick, machine-readable summary. No deep checks."""
    return {
        "kb_dir": str(store.kb_dir),
        "claims": len(store.list_claims()),
        "pages": len(store.list_pages()),
        "sources": len(store.list_sources()),
        "entities": len(store.list_entities()),
        "relations": len(store.list_relations()),
        "evidence": len(store.list_evidence()),
        "sessions": len(store.list_sessions()),
        "pending_proposals": len(store.list_proposals(ProposalStatus.PENDING)),
        "audit_events": count_events(store.kb_dir),
        "index_present": (store.kb_dir / index_db.DB_FILENAME).exists(),
    }


def lint(store: KBStore, *, stale_after_days: int = 180) -> HealthReport:
    findings: list[Finding] = []
    claims = store.list_claims()
    sources_present = {s.id for s in store.list_sources()}
    evidence_present = {e.id for e in store.list_evidence()}

    for c in claims:
        # Citation integrity.
        for ref in c.evidence:
            if ref not in sources_present and ref not in evidence_present:
                findings.append(Finding(
                    "error", "broken_citation",
                    f"claim {c.id} cites missing {ref}", [c.id, ref],
                ))
        # Stale: not confirmed in N days.
        anchor = c.last_confirmed_at or c.updated_at or c.created_at
        if anchor and anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
        if anchor and (datetime.now(UTC) - anchor) > timedelta(days=stale_after_days):
            findings.append(Finding(
                "warning", "stale_claim",
                f"claim {c.id} not confirmed in >{stale_after_days}d",
                [c.id],
            ))
        # Active claims should not be marked contested at the same time.
        if c.status == ClaimStatus.CONTESTED and not c.contradicts:
            findings.append(Finding(
                "warning", "contested_no_contradiction",
                f"claim {c.id} status=contested but no contradicts[] set", [c.id],
            ))

    # Orphan pages (reference a claim that no longer exists).
    claim_ids = {c.id for c in claims}
    for page in store.list_pages():
        for cid in page.claims:
            if cid not in claim_ids:
                findings.append(Finding(
                    "warning", "orphan_page_ref",
                    f"page {page.id} references missing claim {cid}",
                    [page.id, cid],
                ))

    # Dangling relations.
    referable = claim_ids | sources_present | {e.id for e in store.list_entities()} | {
        p.id for p in store.list_pages()
    }
    for rel in store.list_relations():
        for endpoint in (rel.source, rel.target):
            if endpoint not in referable:
                findings.append(Finding(
                    "error", "dangling_relation",
                    f"relation {rel.id} endpoint {endpoint} not found",
                    [rel.id, endpoint],
                ))

    ok = not any(f.severity == "error" for f in findings)
    return HealthReport(ok=ok, findings=findings, counts=status(store))


def doctor(store: KBStore) -> HealthReport:
    """Lint + source verification + index consistency. Slow but thorough."""
    report = lint(store)

    # Source integrity (content hash).
    for vr in verify_all(store):
        if not vr.stored_ok:
            report.findings.append(Finding(
                "error", "source_corrupt",
                f"source {vr.source.id} content hash mismatch",
                [vr.source.id],
            ))
        if vr.external_status == "drift":
            report.findings.append(Finding(
                "warning", "source_drift",
                f"external file {vr.source.locator} changed since registration",
                [vr.source.id],
            ))

    # Config sanity.
    if not store.config_path.exists():
        report.findings.append(Finding(
            "error", "missing_config", "config.yaml is missing",
        ))

    # Index presence (warning only — the index is derivable).
    if not (store.kb_dir / index_db.DB_FILENAME).exists():
        report.findings.append(Finding(
            "info", "index_missing",
            "state.db not present — run `vouch index` to build it",
        ))

    report.ok = not any(f.severity == "error" for f in report.findings)
    return report


def rebuild_index(store: KBStore) -> dict:
    """Drop and rebuild state.db from the durable files. Idempotent."""
    # Detect a stale embedding-model identity before reset() wipes the meta.
    try:
        from . import audit
        from .embeddings.migration import detect_mismatch
        m = detect_mismatch(store.kb_dir)
        if m is not None:
            audit.log_event(
                store.kb_dir, event="embedding.model_mismatch",
                actor="vouch-health",
                object_ids=[], data=m,
            )
    except ImportError:
        pass
    index_db.reset(store.kb_dir)
    with index_db.open_db(store.kb_dir) as conn:
        for c in store.list_claims():
            index_db.index_claim(
                conn, id=c.id, text=c.text,
                type=c.type.value, status=c.status.value, tags=c.tags,
            )
        for p in store.list_pages():
            index_db.index_page(
                conn, id=p.id, title=p.title, body=p.body,
                type=p.type.value, tags=p.tags,
            )
        for e in store.list_entities():
            index_db.index_entity(
                conn, id=e.id, name=e.name, description=e.description,
                type=e.type.value, aliases=e.aliases,
            )
    _rebuild_embeddings(store)
    return index_db.stats(store.kb_dir)


def _rebuild_embeddings(store: KBStore) -> None:
    try:
        from .embeddings import get_embedder
        embedder = get_embedder()
    except Exception:
        return
    with index_db.open_db(store.kb_dir) as conn:
        texts: list[tuple[str, str, str]] = []
        for c in store.list_claims():
            texts.append(("claim", c.id, c.text))
        for p in store.list_pages():
            texts.append(("page", p.id, f"{p.title} {p.body}"))
        for e in store.list_entities():
            texts.append(("entity", e.id, f"{e.name} {e.description or ''}"))
        if not texts:
            return
        batch_size = 64
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            vecs = embedder.encode_batch([t[2] for t in batch])
            for (kind, eid, _), row in zip(batch, vecs, strict=True):
                index_db.index_embedding(conn, kind=kind, id=eid,
                                         vec=row.tolist())


# --- helpers used by `vouch discover` (CLI) -------------------------------

def hash_path(p: Path) -> str:
    return sha256_hex(p.read_bytes()) if p.is_file() else ""
