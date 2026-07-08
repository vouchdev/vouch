"""Claim lifecycle ops: supersede, contradict, archive, cite.

These are *direct* mutations on durable claims — they don't go through the
proposal queue. The rationale: marking a claim as superseded or contradicted
is metadata about reviewed knowledge, not a new assertion. The audit log
captures who did what.

If you want stricter review on lifecycle changes, gate the CLI commands
behind a config flag rather than refactoring this module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from . import audit
from .models import (
    Claim,
    ClaimStatus,
    Evidence,
    Goal,
    GoalStatus,
    Proposal,
    ProposalKind,
    ProposalStatus,
    Relation,
    RelationType,
)
from .storage import ArtifactNotFoundError, KBStore


class LifecycleError(RuntimeError):
    pass


def supersede(
    store: KBStore,
    *,
    old_claim_id: str,
    new_claim_id: str,
    actor: str,
) -> tuple[Claim, Claim]:
    """Mark `old` as superseded by `new`. Both claims must already exist."""
    if old_claim_id == new_claim_id:
        raise LifecycleError("a claim cannot supersede itself")
    old = store.get_claim(old_claim_id)
    new = store.get_claim(new_claim_id)
    rel = Relation(
        id=f"{new.id}--supersedes--{old.id}",
        source=new.id,
        relation=RelationType.SUPERSEDES,
        target=old.id,
    )
    if old.status == ClaimStatus.SUPERSEDED and old.superseded_by == new.id:
        if old.id not in new.supersedes:
            new.supersedes = sorted({*new.supersedes, old.id})
            new.updated_at = datetime.now(UTC)
            store.update_claim(new)
        store.put_relation_idempotent(rel)
        return old, new  # idempotent + convergent retry
    old.status = ClaimStatus.SUPERSEDED
    old.superseded_by = new.id
    old.updated_at = datetime.now(UTC)
    new.supersedes = sorted({*new.supersedes, old.id})
    new.updated_at = datetime.now(UTC)
    # Atomicity: validate both sides before any write so a legacy dangling
    # ref on `new` can't leave `old.superseded_by` written without the
    # reciprocal `new.supersedes` / relation / audit event.
    store._validate_claim_refs(old)
    store._validate_claim_refs(new)
    store.update_claim(old)
    store.update_claim(new)
    # Mirror the supersedes link into the graph for graph-traversal queries.
    store.put_relation_idempotent(rel)
    audit.log_event(
        store.kb_dir, event="claim.supersede", actor=actor,
        object_ids=[old.id, new.id, rel.id],
    )
    return old, new


def contradict(
    store: KBStore,
    *,
    claim_a: str,
    claim_b: str,
    actor: str,
) -> tuple[Claim, Claim, Relation]:
    """Record that two claims contradict each other (symmetric)."""
    a = store.get_claim(claim_a)
    b = store.get_claim(claim_b)
    a.contradicts = sorted({*a.contradicts, b.id})
    b.contradicts = sorted({*b.contradicts, a.id})
    a.status = ClaimStatus.CONTESTED
    b.status = ClaimStatus.CONTESTED
    a.updated_at = b.updated_at = datetime.now(UTC)
    # Atomicity: mirror of supersede — validate both sides before any write.
    store._validate_claim_refs(a)
    store._validate_claim_refs(b)
    store.update_claim(a)
    store.update_claim(b)
    rel = Relation(
        id=f"{a.id}--contradicts--{b.id}",
        source=a.id,
        relation=RelationType.CONTRADICTS,
        target=b.id,
    )
    store.put_relation_idempotent(rel)
    audit.log_event(
        store.kb_dir, event="claim.contradict", actor=actor,
        object_ids=[a.id, b.id, rel.id],
    )
    return a, b, rel


def archive(store: KBStore, *, claim_id: str, actor: str) -> Claim:
    claim = store.get_claim(claim_id)
    claim.status = ClaimStatus.ARCHIVED
    claim.updated_at = datetime.now(UTC)
    store.update_claim(claim)
    audit.log_event(
        store.kb_dir, event="claim.archive", actor=actor, object_ids=[claim.id],
    )
    return claim


def confirm(store: KBStore, *, claim_id: str, actor: str) -> Claim:
    """Re-confirm a stale claim — bumps `last_confirmed_at`."""
    claim = store.get_claim(claim_id)
    claim.last_confirmed_at = datetime.now(UTC)
    claim.updated_at = claim.last_confirmed_at
    if claim.status == ClaimStatus.WORKING:
        claim.status = ClaimStatus.ACTIONABLE
    store.update_claim(claim)
    audit.log_event(
        store.kb_dir, event="claim.confirm", actor=actor, object_ids=[claim.id],
    )
    return claim


def cite(store: KBStore, claim_id: str) -> list[Evidence | dict]:
    """Return resolved citations for a claim.

    Each entry is either an Evidence record (when the citation is an
    Evidence id) or a minimal dict shaped {kind:'source', source_id, title}
    when the citation is a bare Source id.
    """
    claim = store.get_claim(claim_id)
    out: list[Evidence | dict] = []
    for ref in claim.evidence:
        try:
            out.append(store.get_evidence(ref))
            continue
        except ArtifactNotFoundError:
            pass
        try:
            src = store.get_source(ref)
            out.append({
                "kind": "source",
                "source_id": src.id,
                "title": src.title,
                "locator": src.locator,
                "hash": src.hash,
            })
        except ArtifactNotFoundError:
            out.append({"kind": "missing", "ref": ref})
    return out


def goal_set_status(
    store: KBStore,
    *,
    goal_id: str,
    status: GoalStatus | str,
    actor: str,
    reason: str | None = None,
) -> Goal:
    goal: Goal = store.get_goal(goal_id)
    status_val = status.value if isinstance(status, GoalStatus) else status
    try:
        target = GoalStatus(status_val)
    except ValueError as e:
        raise LifecycleError(f"unknown goal status: {status_val}") from e
    if goal.status == target:
        return goal
    goal.status = target
    goal.updated_at = datetime.now(UTC)
    store.update_goal(goal)
    action = f"goal.{target.value}"
    _record_goal_decision_summary(
        store,
        goal_id=goal.id,
        actor=actor,
        action=action,
        reason=reason,
    )
    audit.log_event(
        store.kb_dir,
        event=action,
        actor=actor,
        object_ids=[goal.id],
        data={"reason": reason},
    )
    return goal


def _record_goal_decision_summary(
    store: KBStore,
    *,
    goal_id: str,
    actor: str,
    action: str,
    reason: str | None,
) -> None:
    # Lifecycle transitions are not proposals; write a decided/ summary record
    # so `decided/` stays the durable decision timeline for operators.
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    decided = Proposal(
        id=f"{ts}-goal-{goal_id}-{action.rsplit('.', 1)[-1]}-{uuid.uuid4().hex[:6]}",
        kind=ProposalKind.GOAL,
        proposed_by=actor,
        status=ProposalStatus.APPROVED,
        payload={
            "id": goal_id,
            "action": action,
            "reason": reason,
        },
        decided_at=datetime.now(UTC),
        decided_by=actor,
        decision_reason=reason,
    )
    store.move_proposal_to_decided(decided)
