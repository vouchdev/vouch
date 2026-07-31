"""Claim lifecycle ops: supersede, contradict, archive, cite.

These are *direct* mutations on durable claims — they don't go through the
proposal queue. The rationale: marking a claim as superseded or contradicted
is metadata about reviewed knowledge, not a new assertion. The audit log
captures who did what.

If you want stricter review on lifecycle changes, gate the CLI commands
behind a config flag rather than refactoring this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import audit, index_db
from .models import (
    Claim,
    ClaimStatus,
    Evidence,
    Goal,
    GoalStatus,
    ProposalKind,
    ProposalStatus,
    Relation,
    RelationType,
)
from .proposals import strip_claim_markers
from .storage import ArtifactNotFoundError, KBStore


class LifecycleError(RuntimeError):
    pass


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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
    if claim_a == claim_b:
        raise LifecycleError("a claim cannot contradict itself")
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


def redact(store: KBStore, *, claim_id: str, actor: str) -> Claim:
    """Mask any secret in a claim's text and mark it REDACTED.

    The backstop for a credential that reached a durable claim — pasted before
    capture-time masking existed, or via a path that bypasses it. Rewrites the
    stored yaml so the current tree no longer carries the secret and the claim
    drops out of live retrieval. It does NOT rewrite the audit log or git
    history, which are append-only, so a genuinely leaked credential must still
    be rotated (see docs/security/git-retention.md).
    """
    from .secrets import mask_secrets

    claim = store.get_claim(claim_id)
    claim.text = mask_secrets(claim.text)
    claim.status = ClaimStatus.REDACTED
    claim.updated_at = datetime.now(UTC)
    store.update_claim(claim)
    audit.log_event(
        store.kb_dir, event="claim.redact", actor=actor, object_ids=[claim.id],
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


# Terminal statuses stamp `closed_at`; a goal can still be reopened out of
# them (a "done" migration that turned out not to be done is a real event),
# in which case the stamp is cleared again.
_CLOSED_GOAL_STATUSES = frozenset({GoalStatus.DONE, GoalStatus.ABANDONED})


def cascade_unlink_goal_refs(
    store: KBStore,
    goal_id: str,
    *,
    unlink_claims: list[str] | None = None,
    unlink_entities: list[str] | None = None,
    actor: str,
) -> Goal | None:
    """Drop claim/entity pointers from a goal during cascade delete.

    Companion to ``set_goal_status``: both are the only callers of
    ``store.update_goal``. Pointer edits are not status moves, but they still
    mutate durable goal yaml and must append their own audit event
    (``goal.cascade_unlink``) rather than calling storage from proposals.
    Returns ``None`` when the goal is gone or nothing to unlink (idempotent).
    """
    try:
        goal = store.get_goal(goal_id)
    except ArtifactNotFoundError:
        return None
    claims = [c for c in (unlink_claims or []) if c in goal.claims]
    entities = [e for e in (unlink_entities or []) if e in goal.entities]
    if not claims and not entities:
        return None
    goal.claims = [c for c in goal.claims if c not in claims]
    goal.entities = [e for e in goal.entities if e not in entities]
    goal.updated_at = datetime.now(UTC)
    store.update_goal(goal)
    audit.log_event(
        store.kb_dir,
        event="goal.cascade_unlink",
        actor=actor,
        object_ids=[goal.id],
        data={"claims": claims, "entities": entities},
        reversible=False,
    )
    return goal


def set_goal_status(
    store: KBStore,
    *,
    goal_id: str,
    status: str | GoalStatus,
    actor: str,
    reason: str | None = None,
) -> Goal:
    """Move an approved goal to a new status. The only status write path.

    Same posture as `archive` / `confirm` above: a status move is metadata
    about already-reviewed knowledge, not a new assertion, so it lands
    directly — but it lands *here*, in one place, so every transition appends
    a `goal.status` event to the audit log and a row to the goal's own
    `history`. Nothing else in the codebase may set `Goal.status`; if a future
    change needs to, it belongs in this function. Claim/entity pointer edits
    during cascade delete go through ``cascade_unlink_goal_refs``.
    """
    try:
        new_status = GoalStatus(status)
    except ValueError as e:
        raise LifecycleError(
            f"unknown goal status {status!r}; expected one of "
            f"{[s.value for s in GoalStatus]}"
        ) from e
    goal = store.get_goal(goal_id)
    if goal.status is new_status:
        raise LifecycleError(
            f"goal {goal_id} is already {new_status.value}"
        )
    previous = goal.status
    now = datetime.now(UTC)
    goal.status = new_status
    goal.updated_at = now
    goal.closed_at = now if new_status in _CLOSED_GOAL_STATUSES else None
    goal.history = [
        *goal.history,
        {
            "from": previous.value,
            "to": new_status.value,
            "at": now.isoformat(),
            "actor": actor,
            "reason": reason,
        },
    ]
    store.update_goal(goal)
    audit.log_event(
        store.kb_dir,
        event="goal.status",
        actor=actor,
        object_ids=[goal.id],
        data={"from": previous.value, "to": new_status.value, "reason": reason},
    )
    return goal


def clear_claims(
    store: KBStore,
    *,
    auto_only: bool = True,
    before: datetime | None = None,
    actor: str,
    dry_run: bool = False,
) -> list[Claim]:
    """Clear auto-approved claims, optionally filtered by date range.

    Filters claims by auto-approval status and/or created_at timestamp,
    then archives (not deletes) the matching claims. All operations are
    audited.

    Args:
        store: Knowledge base store
        auto_only: If True, only clear auto-approved claims (auto_approved=True).
                   If False, clear all claims matching date filter.
        before: If set, only clear claims created before this datetime. A naive
                value is read as UTC — `--before 2026-07-01` is the documented
                shape and parses naive, while `created_at` is always aware.
        actor: Who is performing the operation.
        dry_run: If True, don't write changes, just return what would be cleared.

    Returns:
        List of claims that were (or would be) archived.
    """
    cutoff = _utc(before) if before is not None else None
    all_claims = store.list_claims()
    to_clear: list[Claim] = []

    for claim in all_claims:
        # Skip if already archived or status doesn't allow clearing
        if claim.status == ClaimStatus.ARCHIVED:
            continue

        # Filter by auto-approval status
        if auto_only and not claim.auto_approved:
            continue

        # Filter by date range
        if cutoff and _utc(claim.created_at) >= cutoff:
            continue

        to_clear.append(claim)

    # Apply the clear operation (archive the claims)
    if not dry_run:
        for claim in to_clear:
            claim.status = ClaimStatus.ARCHIVED
            claim.updated_at = datetime.now(UTC)
            store.update_claim(claim)

        # Log the bulk operation
        if to_clear:
            audit.log_event(
                store.kb_dir,
                event="claim.bulk_clear",
                actor=actor,
                object_ids=[c.id for c in to_clear],
                data={
                    "count": len(to_clear),
                    "auto_only": auto_only,
                    "before": cutoff.isoformat() if cutoff else None,
                },
            )

    return to_clear


@dataclass
class DeadRefsWipeResult:
    """Outcome of `wipe_dead_claim_refs` (dry-run or apply)."""

    pages: dict[str, list[str]] = field(default_factory=dict)
    proposals: dict[str, list[str]] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def dropped(self) -> int:
        return sum(len(v) for v in self.pages.values()) + sum(
            len(v) for v in self.proposals.values()
        )


def wipe_dead_claim_refs(
    store: KBStore,
    *,
    actor: str,
    dry_run: bool = False,
) -> DeadRefsWipeResult:
    """Strip references to claims that no longer exist, KB-wide.

    Covers durable pages and pending PAGE proposals: the frontmatter claim
    list and the inline `[claim: …]` body markers both lose the dead ids.
    Claims themselves are untouched — an archived claim's file still exists,
    so only ids that resolve to nothing count as dead. One audited bulk
    event records exactly which ids were removed from where.
    """
    result = DeadRefsWipeResult(dry_run=dry_run)
    for page in store.list_pages():
        dead = [c for c in page.claims if not store._claim_path(c).exists()]
        if not dead:
            continue
        result.pages[page.id] = dead
        if dry_run:
            continue
        page.claims = [c for c in page.claims if c not in dead]
        page.body = strip_claim_markers(page.body, dead)
        page.updated_at = datetime.now(UTC)
        store.update_page(page)
        with index_db.open_db(store.kb_dir) as conn:
            index_db.index_page(
                conn, id=page.id, title=page.title, body=page.body,
                type=page.type, tags=page.tags,
            )
    for prop in store.list_proposals(ProposalStatus.PENDING):
        if prop.kind != ProposalKind.PAGE:
            continue
        refs = prop.payload.get("claims") or []
        dead = [c for c in refs if not store._claim_path(c).exists()]
        if not dead:
            continue
        result.proposals[prop.id] = dead
        if dry_run:
            continue
        prop.payload["claims"] = [c for c in refs if c not in dead]
        body = prop.payload.get("body")
        if isinstance(body, str):
            prop.payload["body"] = strip_claim_markers(body, dead)
        store.update_proposal(prop)
    if not dry_run and (result.pages or result.proposals):
        audit.log_event(
            store.kb_dir,
            event="page.dead_refs_wipe",
            actor=actor,
            object_ids=[*result.pages, *result.proposals],
            data={
                "pages": result.pages,
                "proposals": result.proposals,
                "dropped": result.dropped,
            },
        )
    return result


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
