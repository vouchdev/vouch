"""Business logic that bridges Proposals → durable artifacts.

The storage layer is pure CRUD; this module enforces the review gate, the
proposal lifecycle, and writes audit events for every mutation.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml
from pydantic import ValidationError

from . import audit, index_db
from .models import (
    Claim,
    Entity,
    Page,
    Proposal,
    ProposalKind,
    ProposalStatus,
    Relation,
)
from .page_kinds import PageKindError, load_page_kind_registry, validate_page
from .storage import ArtifactNotFoundError, KBStore


class ProposalError(RuntimeError):
    pass


EXPIRE_REASON = "expired"
EXPIRE_ACTOR = "vouch-expire"
_DEFAULT_EXPIRE_PENDING_DAYS = 90


@dataclass
class ExpireResult:
    """Outcome of `expire_pending` (dry-run or apply)."""

    threshold_days: int
    would_expire: list[Proposal] = field(default_factory=list)
    expired: list[Proposal] = field(default_factory=list)


@dataclass
class ProposeClaimResult:
    """Outcome of `propose_claim` including optional similarity warnings."""

    proposal: Proposal
    warnings: list[dict[str, Any]] = field(default_factory=list)

    # Backward-compatible accessors — most callers only need `.id`.
    @property
    def id(self) -> str:
        return self.proposal.id


def new_proposal_id() -> str:
    # Sortable timestamped id: '20260517-143052-<short>'. Sorted listings
    # naturally show oldest pending first, which matches review intuition.
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{ts}-{uuid.uuid4().hex[:8]}"


def _file_proposal(
    store: KBStore,
    *,
    kind: ProposalKind,
    payload: dict[str, Any],
    proposed_by: str,
    session_id: str | None,
    rationale: str | None,
    dry_run: bool,
) -> Proposal:
    proposal = Proposal(
        id=new_proposal_id(),
        kind=kind,
        proposed_by=proposed_by,
        session_id=session_id,
        payload=payload,
        rationale=rationale,
    )
    if dry_run:
        # Dry-run never touches disk. The caller still gets a Proposal back
        # with the id it would have had so the agent can show a preview.
        audit.log_event(
            store.kb_dir, event=f"proposal.{kind.value}.dry_run", actor=proposed_by,
            object_ids=[proposal.id], dry_run=True, data={"payload": payload},
        )
        return proposal
    store.put_proposal(proposal)
    audit.log_event(
        store.kb_dir, event=f"proposal.{kind.value}.create", actor=proposed_by,
        object_ids=[proposal.id], data={"slug_hint": payload.get("id")},
    )
    return proposal


def propose_claim(
    store: KBStore,
    *,
    text: str,
    evidence: list[str],
    proposed_by: str,
    claim_type: str = "observation",
    confidence: float = 0.7,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> ProposeClaimResult:
    if not text.strip():
        raise ProposalError("claim text is empty")
    if not evidence:
        raise ProposalError("claim must cite at least one source or evidence id")
    for eid in evidence:
        try:
            store.get_source(eid)
        except ArtifactNotFoundError:
            try:
                store.get_evidence(eid)
            except ArtifactNotFoundError as e:
                raise ProposalError(f"unknown source/evidence id: {eid}") from e
    claim_id = slug_hint or _slugify(text)
    claim_text = text.strip()
    payload = {
        "id": claim_id,
        "text": claim_text,
        "type": claim_type,
        "confidence": confidence,
        "evidence": list(evidence),
        "entities": entities or [],
        "tags": tags or [],
    }
    exclude_claim: str | None = None
    if (store.kb_dir / "claims" / f"{claim_id}.yaml").exists():
        exclude_claim = claim_id

    warnings: list[dict[str, Any]] = []
    try:
        from .embeddings.similarity import find_similar_on_propose

        warnings = find_similar_on_propose(
            store, claim_text, exclude_claim_id=exclude_claim,
        )
    except ImportError:
        # Base install has no numpy / embeddings extra — propose still works.
        pass

    proposal = _file_proposal(
        store, kind=ProposalKind.CLAIM, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )
    return ProposeClaimResult(proposal=proposal, warnings=warnings)


def propose_page(
    store: KBStore,
    *,
    title: str,
    body: str,
    page_type: str = "concept",
    claim_ids: list[str] | None = None,
    entity_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
    proposed_by: str,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> Proposal:
    if not title.strip():
        raise ProposalError("page title is empty")
    # Mirror the existence check `propose_claim` already runs on evidence
    # ids: a page that lists a claim / entity / source id but never had it
    # resolved is exactly the dangling-reference shape `store.put_page`
    # used to silently accept (issue: graph-integrity write gates).
    for cid in claim_ids or []:
        try:
            store.get_claim(cid)
        except ArtifactNotFoundError as e:
            raise ProposalError(f"unknown claim id: {cid}") from e
    for eid in entity_ids or []:
        try:
            store.get_entity(eid)
        except ArtifactNotFoundError as e:
            raise ProposalError(f"unknown entity id: {eid}") from e
    for sid in source_ids or []:
        try:
            store.get_source(sid)
        except ArtifactNotFoundError as e:
            raise ProposalError(f"unknown source id: {sid}") from e
    meta = metadata or {}
    # Validate the page kind (built-in or config-declared) and its required
    # frontmatter before filing. Raised here so propose-time callers get a
    # per-field error rather than discovering it only at approve.
    try:
        validate_page(
            store,
            page_type,
            meta,
            has_citations=bool(claim_ids or source_ids),
        )
    except PageKindError as e:
        raise ProposalError(str(e)) from e
    payload = {
        "id": slug_hint or _slugify(title),
        "title": title.strip(),
        "body": body,
        "type": page_type,
        "claims": claim_ids or [],
        "entities": entity_ids or [],
        "sources": source_ids or [],
        "tags": tags or [],
        "metadata": meta,
    }
    return _file_proposal(
        store, kind=ProposalKind.PAGE, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )


def propose_entity(
    store: KBStore,
    *,
    name: str,
    entity_type: str,
    aliases: list[str] | None = None,
    description: str | None = None,
    proposed_by: str,
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> Proposal:
    if not name.strip():
        raise ProposalError("entity name is empty")
    payload = {
        "id": slug_hint or _slugify(name),
        "name": name.strip(),
        "type": entity_type,
        "aliases": aliases or [],
        "description": description,
    }
    return _file_proposal(
        store, kind=ProposalKind.ENTITY, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )


def propose_relation(
    store: KBStore,
    *,
    src: str,
    relation: str,
    target: str,
    proposed_by: str,
    confidence: float = 0.7,
    evidence: list[str] | None = None,
    rationale: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> Proposal:
    if not src or not target or not relation:
        raise ProposalError("relation needs src, relation, target")
    # Endpoint + evidence existence checks mirror the `propose_claim`
    # citation loop. The corresponding write-time gate now lives in
    # `store.put_relation` / `store.put_relation_idempotent`; surfacing
    # the same error here means the agent sees a friendly `ProposalError`
    # at proposal time instead of a downstream `ValueError` at approve.
    if not _node_exists(store, src):
        raise ProposalError(
            f"unknown relation source endpoint: {src} (must be an existing "
            f"claim, page, entity, or source id)"
        )
    if not _node_exists(store, target):
        raise ProposalError(
            f"unknown relation target endpoint: {target} (must be an "
            f"existing claim, page, entity, or source id)"
        )
    for eid in evidence or []:
        try:
            store.get_source(eid)
        except ArtifactNotFoundError:
            try:
                store.get_evidence(eid)
            except ArtifactNotFoundError as e:
                raise ProposalError(
                    f"unknown source/evidence id: {eid}"
                ) from e
    rid = f"{src}--{relation}--{target}"
    payload = {
        "id": _slugify(rid),
        "source": src,
        "relation": relation,
        "target": target,
        "confidence": confidence,
        "evidence": evidence or [],
    }
    return _file_proposal(
        store, kind=ProposalKind.RELATION, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )


# --- decisions ------------------------------------------------------------


def _approval_block_reason(
    store: KBStore, proposal: Proposal, approved_by: str
) -> str | None:
    """Why `approved_by` cannot approve `proposal` right now, or None.

    Covers the deterministic pre-write gates — not-pending and
    forbidden_self_approval. Shared by `approve()` and `check_approvable()`
    so the single-approve path and the batch CLI's precheck never drift.
    """
    if proposal.status != ProposalStatus.PENDING:
        return f"proposal {proposal.id} is {proposal.status.value}, not pending"
    if approved_by == proposal.proposed_by:
        # Protected page kinds are exempt from the trusted-agent opt-out:
        # policy-bearing pages (voice, decision records) always need a
        # reviewer other than the proposer, whatever review.approver_role
        # says. Checked first so the opt-out below can never widen it.
        if proposal.kind == ProposalKind.PAGE:
            page_type = str(proposal.payload.get("type", ""))
            if page_type and load_page_kind_registry(store).is_protected(page_type):
                return (
                    f"forbidden_self_approval: page kind '{page_type}' is protected — "
                    "it always requires a reviewer other than the proposer"
                )
        cfg: dict[str, Any] = {}
        try:
            loaded = yaml.safe_load((store.kb_dir / "config.yaml").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            pass
        review_cfg = cfg.get("review")
        approver_role = (
            review_cfg.get("approver_role") if isinstance(review_cfg, dict) else None
        )
        if approver_role != "trusted-agent":
            return (
                f"forbidden_self_approval: {approved_by} cannot approve their own "
                "proposal (set review.approver_role: trusted-agent in config.yaml to opt out)"
            )
    return None


def _payload_block_reason(store: KBStore, proposal: Proposal) -> str | None:
    """Dry-run the put_*-side ref guards, return reason string or None.

    Lets the batch precheck catch dangling refs the write side rejects
    so `vouch approve a b` stays all-or-nothing.
    """
    payload = dict(proposal.payload)
    if proposal.kind == ProposalKind.CLAIM:
        try:
            claim = Claim(**payload)
        except (ValidationError, TypeError) as e:
            return f"invalid claim payload: {e}"
        for ref in claim.evidence:
            if (
                (store._source_dir(ref) / "meta.yaml").exists()
                or store._evidence_path(ref).exists()
            ):
                continue
            return f"claim {claim.id} cites unknown source/evidence {ref!r}"
        try:
            store._validate_claim_refs(claim)
        except ValueError as e:
            return str(e)
    elif proposal.kind == ProposalKind.RELATION:
        try:
            rel = Relation(**payload)
        except (ValidationError, TypeError) as e:
            return f"invalid relation payload: {e}"
        try:
            store._validate_relation_refs(rel)
        except ValueError as e:
            return str(e)
    elif proposal.kind == ProposalKind.PAGE:
        try:
            page = Page(**payload)
        except (ValidationError, TypeError) as e:
            return f"invalid page payload: {e}"
        for cid in page.claims:
            if not store._claim_path(cid).exists():
                return f"page {page.id} references unknown claim {cid}"
        for eid in page.entities:
            if not store._entity_path(eid).exists():
                return f"page {page.id} references unknown entity {eid}"
        for sid in page.sources:
            if not (store._source_dir(sid) / "meta.yaml").exists():
                return f"page {page.id} references unknown source {sid}"
    elif proposal.kind == ProposalKind.ENTITY:
        try:
            Entity(**payload)
        except (ValidationError, TypeError) as e:
            return f"invalid entity payload: {e}"
    return None


def check_approvable(
    store: KBStore, proposal_id: str, *, approved_by: str
) -> str | None:
    """Return why `proposal_id` can't be approved by `approved_by`, or None.

    Read-only. `None` means the deterministic gates pass; the actual write in
    `approve()` can still fail on an I/O error. Used by the batch CLI to
    validate a whole set before mutating anything.
    """
    try:
        proposal = store.get_proposal(proposal_id)
    except ArtifactNotFoundError:
        return f"proposal {proposal_id} not found"
    block = _approval_block_reason(store, proposal, approved_by)
    if block:
        return block
    return _payload_block_reason(store, proposal)


def approve(
    store: KBStore,
    proposal_id: str,
    *,
    approved_by: str,
    reason: str | None = None,
) -> Claim | Page | Entity | Relation:
    """Approve a pending proposal and write it as a durable artifact.

    Raises ProposalError if the proposal is not pending or if
    approved_by matches proposed_by (forbidden_self_approval).
    """
    proposal = store.get_proposal(proposal_id)
    block = _approval_block_reason(store, proposal, approved_by)
    if block:
        raise ProposalError(block)
    payload = dict(proposal.payload)
    # Refuse to overwrite an existing artifact. Without this guard a retry
    # after a crash between put_<kind>() and move_proposal_to_decided() would
    # silently rewrite the artifact with new approved_by / created_at metadata.
    # Exception: PAGE proposals may legitimately target an existing page when
    # filed by vault_to_kb (vault edit flow) — the approve path handles that
    # via update_page rather than put_page.
    if proposal.kind != ProposalKind.PAGE:
        _ensure_no_existing_artifact(store, proposal.kind, payload["id"])
    result: Claim | Page | Entity | Relation
    if proposal.kind == ProposalKind.CLAIM:
        claim = Claim(approved_by=approved_by, **payload)
        store.put_claim(claim)
        with index_db.open_db(store.kb_dir) as conn:
            index_db.index_claim(
                conn, id=claim.id, text=claim.text,
                type=claim.type.value, status=claim.status.value, tags=claim.tags,
            )
        result = claim
    elif proposal.kind == ProposalKind.PAGE:
        page = Page(**payload)
        # Re-validate the kind at the gate: config may have tightened (or a
        # kind been removed) between propose and approve. Built-in kinds pass
        # trivially, so this is a no-op for the common path.
        try:
            validate_page(
                store,
                page.type,
                page.metadata,
                has_citations=bool(page.claims or page.sources),
            )
        except PageKindError as e:
            raise ProposalError(str(e)) from e
        # Vault-edit proposals use slug_hint=page_id so the payload id matches
        # an existing page. In that case update rather than create so the
        # approve path doesn't raise "page already exists" for every normal
        # vault edit. For new pages (no existing artifact) put_page is used
        # as before.
        try:
            store.get_page(page.id)
            store.update_page(page)
        except ArtifactNotFoundError:
            store.put_page(page)
        with index_db.open_db(store.kb_dir) as conn:
            index_db.index_page(
                conn, id=page.id, title=page.title, body=page.body,
                type=page.type, tags=page.tags,
            )
        result = page
        # Lazy import: extractors.edges calls back into propose_relation,
        # so importing it at module scope would be circular.
        from .extractors.edges import auto_propose_edges

        auto_propose_edges(store, page, session_id=proposal.session_id)
    elif proposal.kind == ProposalKind.ENTITY:
        entity = Entity(**payload)
        store.put_entity(entity)
        with index_db.open_db(store.kb_dir) as conn:
            index_db.index_entity(
                conn, id=entity.id, name=entity.name, description=entity.description,
                type=entity.type.value, aliases=entity.aliases,
            )
        result = entity
    else:  # RELATION
        rel = Relation(**payload)
        store.put_relation(rel)
        result = rel

    proposal.status = ProposalStatus.APPROVED
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by = approved_by
    proposal.decision_reason = reason
    store.move_proposal_to_decided(proposal)
    audit.log_event(
        store.kb_dir, event=f"proposal.{proposal.kind.value}.approve",
        actor=approved_by, object_ids=[proposal.id, result.id],
        data={"reason": reason},
    )
    return result


def reject(
    store: KBStore,
    proposal_id: str,
    *,
    rejected_by: str,
    reason: str,
) -> Proposal:
    if not reason.strip():
        raise ProposalError("rejection must include a reason (future agent context)")
    proposal = store.get_proposal(proposal_id)
    if proposal.status != ProposalStatus.PENDING:
        raise ProposalError(
            f"proposal {proposal_id} is {proposal.status.value}, not pending"
        )
    proposal.status = ProposalStatus.REJECTED
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by = rejected_by
    proposal.decision_reason = reason
    store.move_proposal_to_decided(proposal)
    audit.log_event(
        store.kb_dir, event=f"proposal.{proposal.kind.value}.reject",
        actor=rejected_by, object_ids=[proposal.id],
        data={"reason": reason},
    )
    return proposal


def reject_auto_extracted(
    store: KBStore,
    *,
    rejected_by: str,
    page_id: str | None = None,
    reason: str = "auto-extracted edge rejected in bulk",
) -> list[Proposal]:
    """Mass-reject pending edges filed by the auto-extractor.

    Scoped to `AUTO_EXTRACTOR_ACTOR` proposals so this never touches a
    hand-filed relation. `page_id` narrows to edges extracted from one
    originating page (the relation payload's `source`).
    """
    from .extractors.edges import AUTO_EXTRACTOR_ACTOR

    targets = [
        p
        for p in store.list_proposals(ProposalStatus.PENDING)
        if p.kind == ProposalKind.RELATION
        and p.proposed_by == AUTO_EXTRACTOR_ACTOR
        and (page_id is None or p.payload.get("source") == page_id)
    ]
    return [reject(store, p.id, rejected_by=rejected_by, reason=reason) for p in targets]


def expire_pending_after_days(store: KBStore, *, override: int | None = None) -> int:
    """Resolve GC threshold from config (`review.expire_pending_after_days`)."""
    if override is not None:
        return override
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except Exception:
        return _DEFAULT_EXPIRE_PENDING_DAYS
    if not isinstance(loaded, dict):
        return _DEFAULT_EXPIRE_PENDING_DAYS
    review_cfg = loaded.get("review")
    if not isinstance(review_cfg, dict):
        return _DEFAULT_EXPIRE_PENDING_DAYS
    days = review_cfg.get("expire_pending_after_days")
    if isinstance(days, int) and days >= 0:
        return days
    return _DEFAULT_EXPIRE_PENDING_DAYS


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def list_stale_pending(store: KBStore, *, days: int) -> list[Proposal]:
    """Pending proposals older than `days` (by `proposed_at`). `days <= 0` → none."""
    if days <= 0:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stale: list[Proposal] = []
    for proposal in store.list_proposals(ProposalStatus.PENDING):
        if _utc(proposal.proposed_at) < cutoff:
            stale.append(proposal)
    return stale


def expire_one(
    store: KBStore,
    proposal_id: str,
    *,
    expired_by: str = EXPIRE_ACTOR,
) -> Proposal:
    """Expire a single pending proposal (terminal reject + audit)."""
    proposal = store.get_proposal(proposal_id)
    if proposal.status != ProposalStatus.PENDING:
        if (
            proposal.status == ProposalStatus.REJECTED
            and proposal.decision_reason == EXPIRE_REASON
        ):
            return proposal
        raise ProposalError(
            f"proposal {proposal_id} is {proposal.status.value}, not pending"
        )
    proposal.status = ProposalStatus.REJECTED
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by = expired_by
    proposal.decision_reason = EXPIRE_REASON
    store.move_proposal_to_decided(proposal)
    audit.log_event(
        store.kb_dir,
        event="proposal.expire",
        actor=expired_by,
        object_ids=[proposal.id],
        data={"kind": proposal.kind.value},
    )
    return proposal


def expire_pending(
    store: KBStore,
    *,
    apply: bool = False,
    expired_by: str = EXPIRE_ACTOR,
    days: int | None = None,
) -> ExpireResult:
    """Garbage-collect stale pending proposals per review-gate spec."""
    threshold = expire_pending_after_days(store, override=days)
    stale = list_stale_pending(store, days=threshold)
    if not apply:
        return ExpireResult(threshold_days=threshold, would_expire=stale)
    expired = [
        expire_one(store, proposal.id, expired_by=expired_by) for proposal in stale
    ]
    return ExpireResult(
        threshold_days=threshold,
        would_expire=stale,
        expired=expired,
    )


_ARTIFACT_GETTERS = {
    ProposalKind.CLAIM: "get_claim",
    ProposalKind.PAGE: "get_page",
    ProposalKind.ENTITY: "get_entity",
    ProposalKind.RELATION: "get_relation",
}


def _ensure_no_existing_artifact(
    store: KBStore, kind: ProposalKind, artifact_id: str
) -> None:
    getter = getattr(store, _ARTIFACT_GETTERS[kind])
    try:
        getter(artifact_id)
    except ArtifactNotFoundError:
        return
    raise ProposalError(
        f"cannot approve: {kind.value} {artifact_id} already exists "
        f"(a prior approve may have been interrupted; reconcile manually "
        f"by removing the artifact or rejecting this proposal)"
    )


def _node_exists(store: KBStore, node_id: str) -> bool:
    """True if `node_id` resolves to a Claim, Page, Entity, or Source.

    The set of valid Relation endpoint kinds; mirrors
    `KBStore._node_exists` (storage.py) so propose-time and write-time
    rejection use the same definition.
    """
    if not node_id:
        return False
    for getter in (
        store.get_claim,
        store.get_page,
        store.get_entity,
        store.get_source,
    ):
        try:
            getter(node_id)
            return True
        except ArtifactNotFoundError:
            continue
    return False


def _slugify(text: str) -> str:
    out = []
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
