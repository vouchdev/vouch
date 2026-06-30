"""Health checks — `vouch doctor`, `vouch lint`, `vouch status`.

Doctor runs the full sweep (slow, comprehensive). Lint is the subset that
finds *user-actionable* problems: orphan claims, missing citations,
contradictions, stale claims. Status is a one-line summary used by tooling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import index_db
from .audit import count_events, verify_chain
from .models import Claim, ClaimStatus, Entity, Page, ProposalKind, ProposalStatus
from .storage import KBStore, _yaml_load, sha256_hex
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
    # Mixed value types (str/int/bool) — `claims` etc. are ints,
    # `kb_dir` is a str, `index_present` is a bool. Was `dict[str, int]`
    # but `status()` already returned the mixed dict via an untyped
    # `dict` return annotation; the narrow type was effectively never
    # checked. Widened to match runtime reality.
    counts: dict[str, Any] = field(default_factory=dict)


def status(store: KBStore) -> dict[str, Any]:
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


def _safe_counts(store: KBStore, claim_count: int) -> dict:
    """status()-shaped counts without strictly re-loading claims.

    status() calls store.list_claims(), which re-raises on the invalid
    YAMLs that lint/fsck deliberately surface as findings. Pass the
    already-safely-loaded claim count so the report stays self-consistent.
    """
    return {
        "kb_dir": str(store.kb_dir),
        "claims": claim_count,
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


def _load_claims_for_lint(store: KBStore) -> tuple[list[Claim], list[Finding]]:
    """Iterate `claims/*.yaml` one file at a time so a single invalid
    YAML can't crash the whole lint sweep — surface it as a finding
    and keep going. This is the repair hint for KBs that have legacy
    uncited claims from before the Claim.evidence min-citation
    validator landed (#81): `vouch lint` lists them as
    `invalid_claim` findings so the user can fix or delete the file
    rather than seeing a bare `pydantic.ValidationError` traceback."""
    valid: list[Claim] = []
    findings: list[Finding] = []
    cdir = store.kb_dir / "claims"
    if not cdir.is_dir():
        return valid, findings
    for p in sorted(cdir.glob("*.yaml")):
        cid = p.stem
        try:
            valid.append(Claim.model_validate(_yaml_load(p.read_text(encoding="utf-8"))))
        except ValidationError as e:
            tail = str(e).splitlines()[-1].strip() if str(e) else "validation failed"
            findings.append(
                Finding(
                    "error",
                    "invalid_claim",
                    f"claim {cid} ({p}) fails model validation: {tail} — "
                    "edit the YAML to add a citation, or delete the file",
                    [cid],
                )
            )
        except Exception as e:
            findings.append(
                Finding(
                    "error",
                    "unreadable_claim",
                    f"claim {cid} ({p}) could not be loaded: {e}",
                    [cid],
                )
            )
    return valid, findings


def lint(store: KBStore, *, stale_after_days: int = 180) -> HealthReport:
    claims, findings = _load_claims_for_lint(store)
    sources_present = {s.id for s in store.list_sources()}
    evidence_present = {e.id for e in store.list_evidence()}

    for c in claims:
        # Citation integrity.
        for ref in c.evidence:
            if ref not in sources_present and ref not in evidence_present:
                findings.append(
                    Finding(
                        "error",
                        "broken_citation",
                        f"claim {c.id} cites missing {ref}",
                        [c.id, ref],
                    )
                )
        # Stale: not confirmed in N days.
        anchor = c.last_confirmed_at or c.updated_at or c.created_at
        if anchor and anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
        if anchor and (datetime.now(UTC) - anchor) > timedelta(days=stale_after_days):
            findings.append(
                Finding(
                    "warning",
                    "stale_claim",
                    f"claim {c.id} not confirmed in >{stale_after_days}d",
                    [c.id],
                )
            )
        # Active claims should not be marked contested at the same time.
        if c.status == ClaimStatus.CONTESTED and not c.contradicts:
            findings.append(
                Finding(
                    "warning",
                    "contested_no_contradiction",
                    f"claim {c.id} status=contested but no contradicts[] set",
                    [c.id],
                )
            )

    # Orphan pages (reference a claim that no longer exists).
    claim_ids = {c.id for c in claims}
    for page in store.list_pages():
        for cid in page.claims:
            if cid not in claim_ids:
                findings.append(
                    Finding(
                        "warning",
                        "orphan_page_ref",
                        f"page {page.id} references missing claim {cid}",
                        [page.id, cid],
                    )
                )

    # Dangling relations.
    referable = (
        claim_ids
        | sources_present
        | {e.id for e in store.list_entities()}
        | {p.id for p in store.list_pages()}
    )
    for rel in store.list_relations():
        for endpoint in (rel.source, rel.target):
            if endpoint not in referable:
                findings.append(
                    Finding(
                        "error",
                        "dangling_relation",
                        f"relation {rel.id} endpoint {endpoint} not found",
                        [rel.id, endpoint],
                    )
                )

    ok = not any(f.severity == "error" for f in findings)
    return HealthReport(ok=ok, findings=findings, counts=_safe_counts(store, len(claims)))


def doctor(store: KBStore) -> HealthReport:
    """Lint + source verification + index consistency. Slow but thorough."""
    report = lint(store)

    chain = verify_chain(store.kb_dir)
    if not chain.ok:
        detail = f": {chain.reason}" if chain.reason else ""
        report.findings.append(Finding(
            "error", "audit_chain_broken",
            f"audit chain broken at line {chain.line}{detail}",
        ))

    # Source integrity (content hash).
    for vr in verify_all(store):
        if not vr.stored_ok:
            report.findings.append(
                Finding(
                    "error",
                    "source_corrupt",
                    f"source {vr.source.id} content hash mismatch",
                    [vr.source.id],
                )
            )
        if vr.external_status == "drift":
            report.findings.append(
                Finding(
                    "warning",
                    "source_drift",
                    f"external file {vr.source.locator} changed since registration",
                    [vr.source.id],
                )
            )

    # Config sanity.
    if not store.config_path.exists():
        report.findings.append(
            Finding(
                "error",
                "missing_config",
                "config.yaml is missing",
            )
        )

    # Index presence (warning only — the index is derivable).
    if not (store.kb_dir / index_db.DB_FILENAME).exists():
        report.findings.append(
            Finding(
                "info",
                "index_missing",
                "state.db not present — run `vouch index` to build it",
            )
        )

    report.ok = not any(f.severity == "error" for f in report.findings)
    return report


def fsck(store: KBStore) -> HealthReport:
    """Deep consistency check — orphaned embeddings, dangling lifecycle
    chains, decided-proposal ↔ artifact mismatches, index-vs-file drift.

    Read-only; report findings only. `--fix` is intentionally out of scope.
    """
    # Load claims one file at a time (like lint) so a single invalid YAML —
    # e.g. a legacy uncited claim from before #81 — becomes an `invalid_claim`
    # finding instead of aborting the whole check with a traceback. That bad
    # YAML is exactly the kind of inconsistency a deep checker should surface.
    claim_list, findings = _load_claims_for_lint(store)
    claims: dict[str, Claim] = {c.id: c for c in claim_list}
    pages: dict[str, Page] = {p.id: p for p in store.list_pages()}
    entities: dict[str, Entity] = {e.id: e for e in store.list_entities()}

    _check_lifecycle_chains(claims, findings)
    _check_claim_graph_refs(claims, entities, findings)
    _check_decided_proposals(store, claims, pages, entities, findings)

    db_present = (store.kb_dir / index_db.DB_FILENAME).exists()
    if not db_present:
        findings.append(
            Finding(
                "info",
                "index_missing",
                "state.db not present — run `vouch index` to build it",
            )
        )
    else:
        _check_index_drift(store, claims, pages, entities, findings)
        _check_orphan_embeddings(store, claims, pages, entities, findings)

    ok = not any(f.severity == "error" for f in findings)
    return HealthReport(ok=ok, findings=findings, counts=_safe_counts(store, len(claims)))


def _check_lifecycle_chains(
    claims: dict[str, Claim],
    findings: list[Finding],
) -> None:
    """Detect supersede / contradict pointers into the void or out-of-sync.

    Each claim records its lifecycle links inline (`supersedes`,
    `superseded_by`, `contradicts`). Those lists can drift if a referenced
    claim was deleted or if a `contradict` was only written from one side.
    """
    for cid, c in claims.items():
        for target in c.supersedes:
            if target not in claims:
                findings.append(
                    Finding(
                        "error",
                        "dangling_supersedes",
                        f"claim {cid} supersedes missing claim {target}",
                        [cid, target],
                    )
                )
        if c.superseded_by is not None and c.superseded_by not in claims:
            findings.append(
                Finding(
                    "error",
                    "dangling_superseded_by",
                    f"claim {cid} superseded_by missing claim {c.superseded_by}",
                    [cid, c.superseded_by],
                )
            )
        for other in c.contradicts:
            if other not in claims:
                findings.append(
                    Finding(
                        "error",
                        "dangling_contradicts",
                        f"claim {cid} contradicts missing claim {other}",
                        [cid, other],
                    )
                )
                continue
            if cid not in claims[other].contradicts:
                findings.append(
                    Finding(
                        "warning",
                        "asymmetric_contradicts",
                        f"claim {cid} contradicts {other} but {other} does not "
                        f"contradict {cid} back",
                        [cid, other],
                    )
                )


def _check_claim_graph_refs(
    claims: dict[str, Claim],
    entities: dict[str, Entity],
    findings: list[Finding],
) -> None:
    """Detect `claim.entities` pointing at a missing entity.

    Sibling of `_check_lifecycle_chains` for the entity-ref field, so
    legacy KBs surface the blocker before `update_claim` rejects it.
    """
    entity_ids = entities.keys()
    for cid, c in claims.items():
        for eid in c.entities:
            if eid not in entity_ids:
                findings.append(
                    Finding(
                        "error",
                        "dangling_claim_entity",
                        f"claim {cid} references missing entity {eid}",
                        [cid, eid],
                    )
                )


def _check_decided_proposals(
    store: KBStore,
    claims: dict[str, Claim],
    pages: dict[str, Page],
    entities: dict[str, Entity],
    findings: list[Finding],
) -> None:
    """Every approved proposal should have its artifact on disk.

    A crash between `put_<kind>()` and `move_proposal_to_decided()` would
    leave a `decided/` entry without a matching artifact (or vice versa);
    surface the artifact-missing case so an operator can investigate.
    """
    relations = {r.id for r in store.list_relations()}
    presence: dict[ProposalKind, set[str]] = {
        ProposalKind.CLAIM: set(claims),
        ProposalKind.PAGE: set(pages),
        ProposalKind.ENTITY: set(entities),
        ProposalKind.RELATION: relations,
    }
    for pr in store.list_proposals(ProposalStatus.APPROVED):
        artifact_id = pr.payload.get("id") if isinstance(pr.payload, dict) else None
        if not artifact_id:
            findings.append(
                Finding(
                    "error",
                    "decided_no_artifact_id",
                    f"approved proposal {pr.id} has no payload id",
                    [pr.id],
                )
            )
            continue
        if artifact_id not in presence[pr.kind]:
            findings.append(
                Finding(
                    "error",
                    "decided_missing_artifact",
                    f"approved proposal {pr.id} promised "
                    f"{pr.kind.value} {artifact_id} but artifact is missing",
                    [pr.id, artifact_id],
                )
            )


def _check_index_drift(
    store: KBStore,
    claims: dict[str, Claim],
    pages: dict[str, Page],
    entities: dict[str, Entity],
    findings: list[Finding],
) -> None:
    """FTS5 must match disk for every searchable artifact.

    Three drift shapes matter: an indexed row whose artifact is gone, an
    artifact missing from the index entirely (write-hook failure), and a
    `claims_fts.status` value that disagrees with the on-disk
    `claim.status` (the #78 failure mode that leaks archived claims).
    """
    with index_db.open_db(store.kb_dir) as conn:
        indexed_claims = {
            (row[0], row[1]) for row in conn.execute("SELECT id, status FROM claims_fts").fetchall()
        }
        indexed_pages = {row[0] for row in conn.execute("SELECT id FROM pages_fts").fetchall()}
        indexed_entities = {
            row[0] for row in conn.execute("SELECT id FROM entities_fts").fetchall()
        }

    indexed_claim_ids = {cid for cid, _ in indexed_claims}
    _drift_findings("claim", indexed_claim_ids, set(claims), findings)
    _drift_findings("page", indexed_pages, set(pages), findings)
    _drift_findings("entity", indexed_entities, set(entities), findings)

    # Status drift is claim-specific: claims_fts carries a status column that
    # must agree with the on-disk claim (orphans are already reported above).
    for cid, status_in_index in indexed_claims:
        if cid not in claims:
            continue
        on_disk = claims[cid].status.value
        if status_in_index != on_disk:
            findings.append(
                Finding(
                    "error",
                    "index_status_drift",
                    f"claim {cid} status on disk is {on_disk!r} but "
                    f"claims_fts has {status_in_index!r}",
                    [cid],
                )
            )


def _drift_findings(
    kind: str,
    indexed_ids: set[str],
    on_disk_ids: set[str],
    findings: list[Finding],
) -> None:
    """Emit the orphan + missing-row findings for one indexed kind.

    `index_orphan_<kind>` = an FTS5 row whose artifact is gone from disk;
    `index_missing_row` = a durable artifact with no FTS5 row. The shape is
    identical for claims, pages, and entities, so a new kind is a one-liner.
    The FTS5 table is `{kind}s_fts` for every kind.
    """
    for oid in indexed_ids - on_disk_ids:
        findings.append(
            Finding(
                "error",
                f"index_orphan_{kind}",
                f"{kind}s_fts row {oid} has no {kind} on disk",
                [oid],
            )
        )
    for oid in on_disk_ids - indexed_ids:
        findings.append(
            Finding(
                "error",
                "index_missing_row",
                f"{kind} {oid} on disk but missing from {kind}s_fts",
                [oid],
            )
        )


def _check_orphan_embeddings(
    store: KBStore,
    claims: dict[str, Claim],
    pages: dict[str, Page],
    entities: dict[str, Entity],
    findings: list[Finding],
) -> None:
    """Flag embedding rows whose artifact has been deleted.

    Stale vectors are silent: semantic search still returns them, snippets
    just fall back to the bare id. Both the legacy `embeddings` table and
    the newer `embedding_index` table are checked.
    """
    presence = {"claim": set(claims), "page": set(pages), "entity": set(entities)}
    with index_db.open_db(store.kb_dir) as conn:
        # Two tables exist: `embeddings` (legacy) and `embedding_index`
        # (current). Check both — either one drifting silently breaks
        # semantic retrieval.
        for table in ("embeddings", "embedding_index"):
            rows = conn.execute(f"SELECT kind, id FROM {table}").fetchall()
            for kind, eid in rows:
                live = presence.get(kind)
                if live is None or eid in live:
                    continue
                findings.append(
                    Finding(
                        "warning",
                        "orphan_embedding",
                        f"{table} row for {kind} {eid} has no artifact on disk",
                        [eid],
                    )
                )


def rebuild_index(store: KBStore, *, on_progress: Callable[[str], None] | None = None) -> dict:
    """Drop and rebuild state.db from the durable files. Idempotent.

    `on_progress`, if given, is called with a short phase label ("claims",
    "pages", "entities", "embeddings") as each stage starts — for CLI
    progress display. It never affects the result.
    """

    def _tick(phase: str) -> None:
        if on_progress is not None:
            on_progress(phase)

    # Detect a stale embedding-model identity before reset() wipes the meta.
    try:
        from . import audit
        from .embeddings.migration import detect_mismatch

        m = detect_mismatch(store.kb_dir)
        if m is not None:
            audit.log_event(
                store.kb_dir,
                event="embedding.model_mismatch",
                actor="vouch-health",
                object_ids=[],
                data=m,
            )
    except ImportError:
        pass
    index_db.reset(store.kb_dir)
    with index_db.open_db(store.kb_dir) as conn:
        for c in store.list_claims():
            index_db.index_claim(
                conn,
                id=c.id,
                text=c.text,
                type=c.type.value,
                status=c.status.value,
                tags=c.tags,
            )
        for p in store.list_pages():
            index_db.index_page(
                conn,
                id=p.id,
                title=p.title,
                body=p.body,
                type=p.type,
                tags=p.tags,
            )
        for e in store.list_entities():
            index_db.index_entity(
                conn,
                id=e.id,
                name=e.name,
                description=e.description,
                type=e.type.value,
                aliases=e.aliases,
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
            batch = texts[i : i + batch_size]
            vecs = embedder.encode_batch([t[2] for t in batch])
            for (kind, eid, _), row in zip(batch, vecs, strict=True):
                index_db.index_embedding(conn, kind=kind, id=eid, vec=row.tolist())


# --- helpers used by `vouch discover` (CLI) -------------------------------


def hash_path(p: Path) -> str:
    return sha256_hex(p.read_bytes()) if p.is_file() else ""
