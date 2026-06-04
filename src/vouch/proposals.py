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
) -> Proposal:
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
    payload = {
        "id": slug_hint or _slugify(text),
        "text": text.strip(),
        "type": claim_type,
        "confidence": confidence,
        "evidence": list(evidence),
        "entities": entities or [],
        "tags": tags or [],
    }
    return _file_proposal(
        store, kind=ProposalKind.CLAIM, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )


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
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> Proposal:
    if not title.strip():
        raise ProposalError("page title is empty")
    payload = {
        "id": slug_hint or _slugify(title),
        "title": title.strip(),
        "body": body,
        "type": page_type,
        "claims": claim_ids or [],
        "entities": entity_ids or [],
        "sources": source_ids or [],
        "tags": tags or [],
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
        cfg: dict[str, Any] = {}
        try:
            loaded = yaml.safe_load((store.kb_dir / "config.yaml").read_text())
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


def check_approvable(
    store: KBStore, proposal_id: str, *, approved_by: str
) -> str | None:
    """Return why `proposal_id` can't be approved by `approved_by`, or None.

    Read-only. `None` means the deterministic gates pass; the actual write in
    `approve()` can still fail on a pre-existing artifact or an I/O error.
    Used by the batch CLI to validate a whole set before mutating anything.
    """
    try:
        proposal = store.get_proposal(proposal_id)
    except ArtifactNotFoundError:
        return f"proposal {proposal_id} not found"
    return _approval_block_reason(store, proposal, approved_by)


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
        store.put_page(page)
        with index_db.open_db(store.kb_dir) as conn:
            index_db.index_page(
                conn, id=page.id, title=page.title, body=page.body,
                type=page.type.value, tags=page.tags,
            )
        result = page
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


def expire_pending_after_days(store: KBStore, *, override: int | None = None) -> int:
    """Resolve GC threshold from config (`review.expire_pending_after_days`)."""
    if override is not None:
        return override
    try:
        loaded = yaml.safe_load(store.config_path.read_text())
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
