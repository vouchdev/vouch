"""Business logic that bridges Proposals → durable artifacts.

The storage layer is pure CRUD; this module enforces the review gate, the
proposal lifecycle, and writes audit events for every mutation.
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml
from pydantic import ValidationError

from . import admission, audit, index_db
from .config_coerce import coerce_bool
from .models import (
    ArtifactScope,
    Claim,
    Entity,
    Goal,
    GoalStatus,
    Page,
    Proposal,
    ProposalKind,
    ProposalStatus,
    Relation,
    _coerce_artifact_scope,
)
from .page_kinds import PageKindError, load_page_kind_registry, validate_page
from .scoping import viewer_from
from .storage import ArtifactNotFoundError, KBStore


class ProposalError(RuntimeError):
    pass


class DeadClaimRefsError(ProposalError):
    """Approval blocked: the page payload cites claim ids that no longer exist.

    Raised instead of a bare ProposalError so interactive surfaces (CLI
    prompt, console dialog) can detect the case, show the missing ids, and
    retry the approve with drop_missing_claims=True. Claims can disappear
    between propose and approve — archived, redacted, or removed in a bulk
    clear — so this is a normal reviewer decision, not a corrupt proposal.
    """

    def __init__(self, proposal_id: str, missing: list[str]) -> None:
        self.proposal_id = proposal_id
        self.missing = list(missing)
        super().__init__(
            f"page proposal {proposal_id} references missing claim(s): "
            + ", ".join(self.missing)
            + " — approve with drop_missing_claims to strip the dead "
            "references, or reject the proposal"
        )


def strip_claim_markers(body: str, ids: list[str]) -> str:
    """Remove inline `[claim: <id>]` markers for `ids` from a page body."""
    for cid in ids:
        body = re.sub(rf"\s*\[claim:\s*{re.escape(cid)}\]", "", body)
    return body


def missing_claim_refs(store: KBStore, proposal: Proposal) -> list[str]:
    """Claim ids a PAGE proposal cites that don't resolve to a claim file."""
    if proposal.kind != ProposalKind.PAGE:
        return []
    refs = proposal.payload.get("claims") or []
    return [cid for cid in refs if not store._claim_path(cid).exists()]


EXPIRE_REASON = "expired"
EXPIRE_ACTOR = "vouch-expire"
ADMISSION_ACTOR = "vouch-admission"
# Payload flag stamped by propose_page(update_existing=True). Popped before
# Page model validation so it never reaches the durable artifact.
_UPDATE_EXISTING_KEY = "_update_existing"
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


def default_scope(store: KBStore) -> dict[str, str] | None:
    """The stamp every new claim/page proposal carries: this KB's own project.

    Scope cannot be retrofitted once KBs start sharing artifacts, so it is
    recorded at write time. The stamp resolves through the SAME chain the
    read-side viewer uses (``scoping.viewer_from``: VOUCH_PROJECT >
    retrieval.scope > the durable ``kb.id``) so what a KB writes it can
    always read back — including across a ``kb.name`` rename, which is why
    the terminal fallback is the id, never the display name. None (no
    stamp, today's behaviour) for KBs with no identity and no configured
    scope.
    """
    project = viewer_from(config_path=store.config_path).project
    if project is None:
        return None
    return {"visibility": "project", "project": project}


def _stamp_scope(
    store: KBStore, payload: dict[str, Any], scope: dict[str, Any] | str | None
) -> None:
    """Attach an explicit scope, or the KB's own default, to a payload.

    An explicit scope is validated here, at the gate: a malformed one filed
    into a payload would otherwise crash every audit read surface (which
    resolves payload scopes for visibility) and escape ``approve()`` as a
    raw pydantic error.
    """
    if scope is None:
        stamp = default_scope(store)
        if stamp is not None:
            payload["scope"] = stamp
        return
    try:
        coerced = _coerce_artifact_scope(scope)
        validated = coerced if isinstance(coerced, ArtifactScope) \
            else ArtifactScope.model_validate(coerced)
    except (ValidationError, ValueError) as e:
        raise ProposalError(f"invalid scope: {e}") from e
    payload["scope"] = validated.model_dump(mode="json")


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
    # Admission gate: deterministic, receipt-safe floor on knowledge-shaped
    # garbage. It only *blocks* the passive auto-capture firehoses; a deliberate
    # author's write is advisory-only and passes straight through to the review
    # gate. Filing-then-rejecting (rather than dropping) keeps the audit log as
    # the authoritative record of what was refused and why.
    if proposed_by in admission.AUTO_CAPTURE_ACTORS:
        verdict = admission.assess(kind.value, payload, admission.load_config(store))
        if not verdict.admit:
            return reject(
                store, proposal.id,
                rejected_by=ADMISSION_ACTOR,
                reason=f"admission: {verdict.reason}",
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
    scope: dict[str, Any] | str | None = None,
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
    payload: dict[str, Any] = {
        "id": claim_id,
        "text": claim_text,
        "type": claim_type,
        "confidence": confidence,
        "evidence": list(evidence),
        "entities": entities or [],
        "tags": tags or [],
    }
    _stamp_scope(store, payload, scope)
    # Validate against the Claim model itself (same check approve()'s
    # Claim(**payload) construction and the batch precheck in
    # _payload_block_reason both already perform) so an out-of-range
    # confidence or other model-level constraint violation is rejected here,
    # at propose time, rather than filing a proposal that can never pass
    # approve() and sits stuck in the pending queue until someone notices.
    try:
        Claim(**payload)
    except (ValidationError, TypeError) as e:
        raise ProposalError(f"invalid claim payload: {e}") from e
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


def propose_quoted_claim(
    store: KBStore,
    *,
    text: str,
    source_id: str,
    quote: str,
    proposed_by: str,
    claim_type: str = "observation",
    confidence: float = 0.7,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    scope: dict[str, Any] | str | None = None,
) -> ProposeClaimResult | None:
    """File a claim backed by a byte-offset receipt into ``source_id``, or drop.

    Locates ``quote`` verbatim in the source's raw bytes; if it is not there,
    returns None and files nothing — the mechanical "drops any claim it cannot
    quote." Otherwise stores a receipt-backed Evidence (idempotently, keyed on
    the span) and files a normal claim proposal citing it, so the write still
    goes through the review gate but now carries a receipt the gate can verify
    by string comparison.
    """
    from . import receipts

    source_bytes = store.read_source_content(source_id)
    evidence = receipts.receipt_for_quote(
        source_id=source_id, source_bytes=source_bytes, quote=quote,
    )
    if evidence is None:
        return None
    # deterministic id -> if this span is already stored, cite the existing
    # Evidence rather than duplicating it.
    with contextlib.suppress(ValueError):
        store.put_evidence(evidence)
    return propose_claim(
        store, text=text, evidence=[evidence.id], proposed_by=proposed_by,
        claim_type=claim_type, confidence=confidence, entities=entities,
        tags=tags, rationale=rationale, slug_hint=slug_hint,
        session_id=session_id, scope=scope,
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
    metadata: dict[str, Any] | None = None,
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
    scope: dict[str, Any] | str | None = None,
    update_existing: bool = False,
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
    page_id = slug_hint or _slugify(title)
    # Opt-in update path for vault_to_kb (#219). Without this flag, approve()
    # refuses an existing page id the same way it refuses claim collisions —
    # otherwise a colliding title/slug (or a malicious slug_hint) silently
    # overwrites durable content via update_page.
    if update_existing:
        try:
            store.get_page(page_id)
        except ArtifactNotFoundError as e:
            raise ProposalError(
                f"cannot update: page {page_id} does not exist"
            ) from e
    payload: dict[str, Any] = {
        "id": page_id,
        "title": title.strip(),
        "body": body,
        "type": page_type,
        "claims": claim_ids or [],
        "entities": entity_ids or [],
        "sources": source_ids or [],
        "tags": tags or [],
        "metadata": meta,
    }
    if update_existing:
        payload[_UPDATE_EXISTING_KEY] = True
    _stamp_scope(store, payload, scope)
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
    payload: dict[str, Any] = {
        "id": slug_hint or _slugify(name),
        "name": name.strip(),
        "type": entity_type,
        "aliases": aliases or [],
        "description": description,
    }
    # Validate against the Entity model itself (same check approve()'s
    # Entity(**payload) construction and the batch precheck in
    # _payload_block_reason both already perform) so an invalid entity type
    # is rejected here, at propose time, rather than filing a proposal that
    # can never pass approve() and sits stuck in the pending queue until
    # someone notices.
    try:
        Entity(**payload)
    except (ValidationError, TypeError) as e:
        raise ProposalError(f"invalid entity payload: {e}") from e
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
    payload: dict[str, Any] = {
        "id": _slugify(rid),
        "source": src,
        "relation": relation,
        "target": target,
        "confidence": confidence,
        "evidence": evidence or [],
    }
    # Validate against the Relation model itself (same check approve()'s
    # Relation(**payload) construction and the batch precheck in
    # _payload_block_reason both already perform) so an out-of-range
    # confidence or an invalid relation type is rejected here, at propose
    # time, rather than filing a proposal that can never pass approve() and
    # sits stuck in the pending queue until someone notices.
    try:
        Relation(**payload)
    except (ValidationError, TypeError) as e:
        raise ProposalError(f"invalid relation payload: {e}") from e
    return _file_proposal(
        store, kind=ProposalKind.RELATION, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )


def propose_goal(
    store: KBStore,
    *,
    title: str,
    proposed_by: str,
    detail: str | None = None,
    claims: list[str] | None = None,
    entities: list[str] | None = None,
    tags: list[str] | None = None,
    scope: dict[str, Any] | str | None = None,
    rationale: str | None = None,
    slug_hint: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> Proposal:
    """File a review-gated objective. Approving it creates the `open` goal.

    A goal is knowledge about intent, so it takes the same route as every
    other write: pending proposal → human approve → durable yaml. There is
    deliberately no direct-write entry point, and the payload is pinned to
    `status: open` — a proposal cannot land a goal that is already `done`,
    which would put a transition on disk that never passed through
    `lifecycle.set_goal_status` and so never reached the audit log.
    """
    if not title.strip():
        raise ProposalError("goal title is empty")
    for cid in claims or []:
        if not store._claim_path(cid).exists():
            raise ProposalError(f"unknown claim id: {cid}")
    for eid in entities or []:
        if not store._entity_path(eid).exists():
            raise ProposalError(f"unknown entity id: {eid}")
    payload: dict[str, Any] = {
        "id": slug_hint or _slugify(title),
        "title": title.strip(),
        "detail": detail,
        "status": GoalStatus.OPEN.value,
        "claims": list(claims or []),
        "entities": list(entities or []),
        "tags": list(tags or []),
    }
    _stamp_scope(store, payload, scope)
    # Validate against the model here, at propose time, for the same reason
    # propose_entity does: a payload that can never pass approve() must not
    # sit in the pending queue waiting for someone to notice.
    try:
        Goal(**payload)
    except (ValidationError, TypeError) as e:
        raise ProposalError(f"invalid goal payload: {e}") from e
    return _file_proposal(
        store, kind=ProposalKind.GOAL, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )


def propose_delete(
    store: KBStore,
    *,
    target_kind: str,
    target_id: str,
    proposed_by: str,
    rationale: str | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
    cascade: bool = False,
) -> Proposal:
    """File a review-gated request to hard-delete a durable artifact.

    Blocked (at propose time, re-checked at approve) if the target is still
    referenced by another artifact — the maintainer must supersede or remove
    the referrers first. The full artifact is snapshotted into the payload so
    the decided proposal and audit event record exactly what was removed.

    `cascade=True` lifts that block by making the referrers part of the
    proposal instead of a prerequisite for it: the required referrer edits
    are recorded in the payload, the reviewer approves the whole set as one
    decision, and `_approve_delete` re-derives and applies them before the
    delete. Default off, so every existing caller keeps today's behaviour.
    """
    if target_kind not in _DELETE_KINDS:
        raise ProposalError(
            f"unknown target_kind {target_kind!r}; expected one of "
            f"{sorted(_DELETE_KINDS)}"
        )
    getter = getattr(store, _DELETE_GETTERS[target_kind])
    try:
        artifact = getter(target_id)
    except ArtifactNotFoundError as e:
        raise ProposalError(f"unknown {target_kind} id: {target_id}") from e
    refs = referenced_by(store, target_kind, target_id)
    if refs and not cascade:
        hint = " (supersede it instead?)" if target_kind == "claim" else ""
        raise ProposalError(
            f"cannot delete {target_kind} {target_id}: referenced by "
            + ", ".join(refs)
            + hint
            + " — or re-file with cascade to include the referrer edits "
            "in this proposal (CLI: --cascade)"
        )
    payload: dict[str, Any] = {
        "target_kind": target_kind,
        "id": target_id,
        "snapshot": artifact.model_dump(mode="json"),
    }
    if cascade:
        payload["cascade"] = cascade_plan(store, target_kind, target_id)
    return _file_proposal(
        store, kind=ProposalKind.DELETE, payload=payload,
        proposed_by=proposed_by, session_id=session_id,
        rationale=rationale, dry_run=dry_run,
    )


# --- decisions ------------------------------------------------------------


def _review_config(store: KBStore) -> dict[str, Any]:
    """the ``review:`` section of config.yaml, or {} if absent/unreadable.

    ``auto_approve_on_receipt`` is normalized to a real bool here -- the
    single source of truth every caller below reads from -- so a
    mistakenly-quoted ``auto_approve_on_receipt: "false"`` in config.yaml
    can never be silently treated as enabled by one call site while another
    (correctly) treats the same value as disabled. callers that still wrap
    this in their own ``bool(...)`` are unaffected: bool() on an already-real
    bool is a no-op.
    """
    try:
        loaded = yaml.safe_load(
            (store.kb_dir / "config.yaml").read_text(encoding="utf-8")
        )
    except Exception:
        return {}
    if isinstance(loaded, dict) and isinstance(loaded.get("review"), dict):
        review = dict(loaded["review"])
        review["auto_approve_on_receipt"] = coerce_bool(
            review.get("auto_approve_on_receipt"), False
        )
        return review
    return {}


def _claim_receipts_verify(store: KBStore, proposal: Proposal) -> bool:
    """True if this CLAIM proposal's citations all carry receipts that verify."""
    from . import receipts

    evidence_ids = list(proposal.payload.get("evidence", []))
    return receipts.evaluate_claim_receipts(store, evidence_ids).approve


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
        # Protected page kinds are exempt from every self-approval opt-out:
        # policy-bearing pages (voice, decision records) always need a
        # reviewer other than the proposer. Checked first so nothing below
        # can widen it.
        if proposal.kind == ProposalKind.PAGE:
            page_type = str(proposal.payload.get("type", ""))
            if page_type and load_page_kind_registry(store).is_protected(page_type):
                return (
                    f"forbidden_self_approval: page kind '{page_type}' is protected — "
                    "it always requires a reviewer other than the proposer"
                )
        review_cfg = _review_config(store)
        # Blanket opt-out: trust the agent for everything.
        if review_cfg.get("approver_role") == "trusted-agent":
            return None
        # Phase D: the receipt is the reviewer. A claim whose byte-offset
        # receipts all verify needs no human — the mechanical check already
        # confirmed the quoted span is in the source. A claim that cannot quote
        # its source (bare source id, forged or missing receipt) does not
        # qualify and still falls through to the human gate below.
        if (
            review_cfg.get("auto_approve_on_receipt")
            and proposal.kind == ProposalKind.CLAIM
            and _claim_receipts_verify(store, proposal)
        ):
            return None
        return (
            f"forbidden_self_approval: {approved_by} cannot approve their own "
            "proposal (set review.approver_role: trusted-agent, or "
            "review.auto_approve_on_receipt for receipt-backed claims)"
        )
    return None


def resolve_pending_receipt_claim(
    store: KBStore, proposal: Proposal, *, actor: str, reason: str
) -> Claim | None:
    """Mechanically decide one pending CLAIM proposal, honouring the gate.

    Returns the durable Claim when self-approval clears — under
    ``review.approver_role: trusted-agent``, or when the claim's byte-offset
    receipts all verify under ``review.auto_approve_on_receipt``. Returns None
    when the proposal stays pending (gate closed, receipts unverifiable, or
    its id is held by a claim with *different* text — a real conflict, a human
    call) or when it was rejected as a duplicate: re-deriving a claim whose
    identical text is already durable adds nothing, so the proposal is closed
    with a duplicate reason instead of piling up in the review queue.
    """
    if proposal.kind != ProposalKind.CLAIM:
        return None
    if proposal.status is not ProposalStatus.PENDING:
        # The admission gate may have auto-rejected this proposal at filing time
        # (file-then-reject in _file_proposal). A decided proposal has nothing to
        # resolve — approving it would raise. Skip so the capture loop survives a
        # rejected fragment and still approves the good claims beside it.
        return None
    review_cfg = _review_config(store)
    trusted = review_cfg.get("approver_role") == "trusted-agent"
    receipted = bool(
        review_cfg.get("auto_approve_on_receipt")
    ) and _claim_receipts_verify(store, proposal)
    if not (trusted or receipted):
        return None
    claim_id = str(proposal.payload.get("id", ""))
    existing: Claim | None = None
    if claim_id:
        try:
            existing = store.get_claim(claim_id)
        except ArtifactNotFoundError:
            existing = None
    if existing is not None:
        if existing.text == proposal.payload.get("text"):
            reject(
                store, proposal.id, rejected_by=actor,
                reason="duplicate: identical claim already durable",
            )
        return None
    result = approve(store, proposal.id, approved_by=actor, reason=reason)
    assert isinstance(result, Claim)  # kind == CLAIM guaranteed above
    return result


def auto_approve_receipts(
    store: KBStore, *, actor: str | None = None
) -> list[Claim]:
    """Approve every pending receipt-verified claim, no human in the loop.

    The mechanical gate is the reviewer: a pending CLAIM whose citations all
    carry receipts that verify by string comparison is approved; a duplicate
    of an already-durable identical claim is rejected (see
    ``resolve_pending_receipt_claim``); anything else — a bare source id, a
    forged or missing receipt, a non-claim proposal, an id held by different
    text — is left pending for a human. This is the drain that makes "run
    vouch and it just captures knowledge" real. No-op unless
    ``review.auto_approve_on_receipt`` is set, so the human-review gate is
    never silently bypassed.
    """
    if not _review_config(store).get("auto_approve_on_receipt"):
        return []
    approved: list[Claim] = []
    for proposal in store.list_proposals(ProposalStatus.PENDING):
        claim = resolve_pending_receipt_claim(
            store, proposal,
            actor=actor or proposal.proposed_by,
            reason="receipt verified — auto-approved",
        )
        if claim is not None:
            approved.append(claim)
    return approved


def auto_approve_pending(
    store: KBStore, *, actor: str | None = None
) -> list[Claim | Page | Entity | Relation | Goal]:
    """Approve every pending proposal the configured gate allows.

    The full drain behind auto-approval-by-default. Under
    ``review.approver_role: trusted-agent`` every pending proposal
    self-approves through the normal ``approve()`` path — one audit event
    per artifact, never a parallel write path. Claims go through
    ``resolve_pending_receipt_claim`` so duplicates of durable claims are
    closed instead of piling up; pages, entities and relations are approved
    unless something still blocks them. What stays pending is exactly the
    human-call residue: protected page kinds, pages with dead claim
    references, an id already durable (pages included — colliding page
    proposals no longer overwrite via update_page), and DELETE
    proposals (retracting durable knowledge is never drained mechanically).

    Without trusted-agent this falls back to the receipt drain
    (``auto_approve_receipts``), which is itself a no-op when
    ``review.auto_approve_on_receipt`` is off — the review gate is honoured,
    never silently bypassed.
    """
    review_cfg = _review_config(store)
    if review_cfg.get("approver_role") != "trusted-agent":
        return list(auto_approve_receipts(store, actor=actor))
    approved: list[Claim | Page | Entity | Relation | Goal] = []
    for proposal in store.list_proposals(ProposalStatus.PENDING):
        if proposal.kind == ProposalKind.DELETE:
            continue
        if proposal.kind == ProposalKind.CLAIM:
            claim = resolve_pending_receipt_claim(
                store, proposal,
                actor=actor or proposal.proposed_by,
                reason="trusted-agent — auto-approved",
            )
            if claim is not None:
                approved.append(claim)
            continue
        approver = actor or proposal.proposed_by
        if check_approvable(store, proposal.id, approved_by=approver) is not None:
            continue
        try:
            approved.append(
                approve(
                    store, proposal.id, approved_by=approver,
                    reason="trusted-agent — auto-approved",
                )
            )
        except ProposalError:
            # check_approvable is a dry-run; the write itself can still fail
            # (e.g. a claim ref deleted between check and approve). Left
            # pending for a human, the drain survives.
            continue
    return approved


def _payload_block_reason(
    store: KBStore, proposal: Proposal, *, skip_dead_claim_refs: bool = False
) -> str | None:
    """Dry-run the put_*-side ref guards, return reason string or None.

    Lets the batch precheck catch dangling refs the write side rejects
    so `vouch approve a b` stays all-or-nothing.

    `skip_dead_claim_refs` omits the page→claim existence check. `approve()`
    sets it because a dead claim ref is a reviewer decision there, handled by
    the DeadClaimRefsError / drop_missing_claims path — not a flat block.
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
        page_payload = dict(payload)
        is_update = bool(page_payload.pop(_UPDATE_EXISTING_KEY, False))
        try:
            page = Page(**page_payload)
        except (ValidationError, TypeError) as e:
            return f"invalid page payload: {e}"
        if is_update:
            try:
                store.get_page(page.id)
            except ArtifactNotFoundError:
                return f"cannot update: page {page.id} does not exist"
        if not skip_dead_claim_refs:
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
    elif proposal.kind == ProposalKind.GOAL:
        try:
            goal = Goal(**payload)
        except (ValidationError, TypeError) as e:
            return f"invalid goal payload: {e}"
        if goal.status is not GoalStatus.OPEN:
            return (
                f"goal {goal.id} would be approved into status "
                f"{goal.status.value!r}; only 'open' may be approved — later "
                "moves go through lifecycle.set_goal_status"
            )
        try:
            store._validate_goal_refs(goal)
        except ValueError as e:
            return str(e)
    elif proposal.kind == ProposalKind.DELETE:
        target_kind = str(payload.get("target_kind", ""))
        target_id = str(payload.get("id", ""))
        if target_kind not in _DELETE_KINDS:
            return f"invalid delete target_kind: {target_kind!r}"
        getter = getattr(store, _DELETE_GETTERS[target_kind])
        try:
            getter(target_id)
        except ArtifactNotFoundError:
            return None  # already gone → idempotent approve is fine
        refs = referenced_by(store, target_kind, target_id)
        if refs and "cascade" not in payload:
            return (
                f"cannot delete {target_kind} {target_id}: referenced by "
                + ", ".join(refs)
            )
        # A cascade proposal is *expected* to have referrers — unlinking them
        # is what the reviewer approved. `_approve_delete` re-derives and
        # applies the plan, then the approve-time `referenced_by` re-check
        # still has to come back empty before anything is deleted.
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
    drop_missing_claims: bool = False,
) -> Claim | Page | Entity | Relation | Goal:
    """Approve a pending proposal and write it as a durable artifact.

    Raises ProposalError if the proposal is not pending or if
    approved_by matches proposed_by (forbidden_self_approval).

    A PAGE proposal citing claim ids that no longer exist raises
    DeadClaimRefsError so the reviewer can decide: retry with
    drop_missing_claims=True to strip the dead references (frontmatter list
    and inline `[claim: …]` markers) and approve what remains — the dropped
    ids are recorded in the audit event.
    """
    proposal = store.get_proposal(proposal_id)
    block = _approval_block_reason(store, proposal, approved_by)
    if block:
        raise ProposalError(block)
    block = _payload_block_reason(store, proposal, skip_dead_claim_refs=True)
    if block:
        raise ProposalError(block)
    payload = dict(proposal.payload)
    dropped_claims: list[str] = []
    # Refuse to overwrite an existing artifact. Without this guard a retry
    # after a crash between put_<kind>() and move_proposal_to_decided() would
    # silently rewrite the artifact with new approved_by / created_at metadata.
    # PAGE updates are opt-in via propose_page(update_existing=True) — used by
    # vault_to_kb (#219). A bare colliding propose_page must not take the
    # update_page path or durable content (and scope) is silently wiped.
    is_page_update = (
        proposal.kind == ProposalKind.PAGE
        and bool(payload.get(_UPDATE_EXISTING_KEY))
    )
    if proposal.kind != ProposalKind.DELETE and not is_page_update:
        _ensure_no_existing_artifact(store, proposal.kind, payload["id"])
    result: Claim | Page | Entity | Relation | Goal
    if proposal.kind == ProposalKind.CLAIM:
        is_auto_approved = approved_by == proposal.proposed_by
        claim = Claim(
            approved_by=approved_by,
            proposed_by=proposal.proposed_by,
            auto_approved=is_auto_approved,
            **payload
        )
        store.put_claim(claim)
        with index_db.open_db(store.kb_dir) as conn:
            index_db.index_claim(
                conn, id=claim.id, text=claim.text,
                type=claim.type.value, status=claim.status.value, tags=claim.tags,
            )
        result = claim
    elif proposal.kind == ProposalKind.PAGE:
        missing = missing_claim_refs(store, proposal)
        if missing:
            if not drop_missing_claims:
                raise DeadClaimRefsError(proposal.id, missing)
            payload["claims"] = [
                c for c in (payload.get("claims") or []) if c not in missing
            ]
            if isinstance(payload.get("body"), str):
                payload["body"] = strip_claim_markers(payload["body"], missing)
            dropped_claims = missing
        is_update = bool(payload.pop(_UPDATE_EXISTING_KEY, False))
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
        # Opt-in update (vault edit) vs create. Existence for updates was
        # already checked in _payload_block_reason / the collision guard.
        if is_update:
            store.update_page(page)
        else:
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
    elif proposal.kind == ProposalKind.GOAL:
        goal = Goal(approved_by=approved_by, **payload)
        store.put_goal(goal)
        result = goal
    elif proposal.kind == ProposalKind.DELETE:
        result = _approve_delete(store, proposal, approved_by=approved_by)
    else:  # RELATION
        rel = Relation(**payload)
        store.put_relation(rel)
        result = rel

    proposal.status = ProposalStatus.APPROVED
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by = approved_by
    proposal.decision_reason = reason
    # Audit before the decided-move: the log is the authoritative history, so
    # a crash between the two must leave a pending proposal WITH its decision
    # event (recoverable; retry is blocked by _ensure_no_existing_artifact),
    # never a decided proposal without one.
    decision_data: dict[str, Any] = {"reason": reason}
    if dropped_claims:
        decision_data["dropped_claims"] = dropped_claims
    audit.log_event(
        store.kb_dir, event=f"proposal.{proposal.kind.value}.approve",
        actor=approved_by, object_ids=[proposal.id, result.id],
        data=decision_data,
    )
    store.move_proposal_to_decided(proposal)
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
    # Audit before the decided-move — same ordering invariant as approve().
    audit.log_event(
        store.kb_dir, event=f"proposal.{proposal.kind.value}.reject",
        actor=rejected_by, object_ids=[proposal.id],
        data={"reason": reason},
    )
    store.move_proposal_to_decided(proposal)
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
    # Audit before the decided-move — same ordering invariant as approve().
    audit.log_event(
        store.kb_dir,
        event="proposal.expire",
        actor=expired_by,
        object_ids=[proposal.id],
        data={"kind": proposal.kind.value},
    )
    store.move_proposal_to_decided(proposal)
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
    ProposalKind.GOAL: "get_goal",
}


_DELETE_KINDS = {"claim", "page", "entity", "relation"}

_DELETE_GETTERS = {
    "claim": "get_claim",
    "page": "get_page",
    "entity": "get_entity",
    "relation": "get_relation",
}


def _relation_endpoint_kind(store: KBStore, node_id: str) -> str | None:
    """Resolve a bare relation endpoint id to an artifact kind.

    Mirrors ``KBStore._node_exists`` priority (claim → page → entity →
    source) so same-slug collisions pick a single kind. Used by
    ``referenced_by`` so a claim↔claim edge cannot block deleting a page
    that happens to share the slug (#663 / #600 carve-out).
    """
    if not node_id:
        return None
    if store._claim_path(node_id).exists():
        return "claim"
    if store._page_path(node_id).exists():
        return "page"
    if store._entity_path(node_id).exists():
        return "entity"
    if (store._source_dir(node_id) / "meta.yaml").exists():
        return "source"
    return None


def _relation_refers_to(
    store: KBStore, rel: Relation, target_kind: str, target_id: str,
) -> bool:
    """True when ``rel`` endpoints the target id *as* ``target_kind``."""
    for endpoint in (rel.source, rel.target):
        if endpoint == target_id and (
            _relation_endpoint_kind(store, endpoint) == target_kind
        ):
            return True
    return False


def referenced_by(store: KBStore, target_kind: str, target_id: str) -> list[str]:
    """Inbound referrers to `target_id` — the "block if referenced" gate.

    Returns human-readable descriptions of artifacts that point AT the
    target. Only inbound refs count; outbound refs (what the target itself
    points at) are never returned, because deleting the holder simply drops
    its own pointers. An empty list means the artifact is safe to delete.
    """
    if target_kind not in _DELETE_KINDS:
        raise ProposalError(
            f"unknown target_kind {target_kind!r}; expected one of "
            f"{sorted(_DELETE_KINDS)}"
        )
    refs: list[str] = []
    if target_kind == "claim":
        for page in store.list_pages():
            if target_id in page.claims:
                refs.append(f"page {page.id!r}")
        for goal in store.list_goals():
            if target_id in goal.claims:
                refs.append(f"goal {goal.id!r}")
        for rel in store.list_relations():
            if _relation_refers_to(store, rel, target_kind, target_id):
                refs.append(f"relation {rel.id!r}")
        for claim in store.list_claims():
            if claim.id == target_id:
                continue
            if (
                target_id in claim.supersedes
                or claim.superseded_by == target_id
                or target_id in claim.contradicts
            ):
                refs.append(f"claim {claim.id!r}")
    elif target_kind == "page":
        for rel in store.list_relations():
            if _relation_refers_to(store, rel, target_kind, target_id):
                refs.append(f"relation {rel.id!r}")
    elif target_kind == "entity":
        for claim in store.list_claims():
            if target_id in claim.entities:
                refs.append(f"claim {claim.id!r}")
        for page in store.list_pages():
            if target_id in page.entities:
                refs.append(f"page {page.id!r}")
        for goal in store.list_goals():
            if target_id in goal.entities:
                refs.append(f"goal {goal.id!r}")
        for rel in store.list_relations():
            if _relation_refers_to(store, rel, target_kind, target_id):
                refs.append(f"relation {rel.id!r}")
    # target_kind == "relation": edges have no inbound refs → refs stays empty
    return refs


def _relation_cascade_steps(store: KBStore, target_id: str) -> list[dict[str, Any]]:
    return [
        {"kind": "relation", "id": rel.id, "action": "delete"}
        for rel in store.list_relations()
        if target_id in (rel.source, rel.target)
    ]


def cascade_plan(
    store: KBStore, target_kind: str, target_id: str
) -> list[dict[str, Any]]:
    """The referrer edits that would let `target_id` be deleted.

    Structured mirror of `referenced_by`, walked in the same order, so the
    plan a reviewer approves lines up with the refusal that sent them here.
    One step per referring artifact: a page citing the target in both its
    frontmatter and its body is one decision, not two.

    Pages, claims, and goals lose their pointer; relations are deleted
    outright, because an edge whose endpoint is gone has no meaning.
    Relations carry no inbound refs of their own (`referenced_by` returns
    [] for them), so the walk is one level deep by construction — there is
    no transitive cascade to bound.
    """
    if target_kind not in _DELETE_KINDS:
        raise ProposalError(
            f"unknown target_kind {target_kind!r}; expected one of "
            f"{sorted(_DELETE_KINDS)}"
        )
    steps: list[dict[str, Any]] = []
    if target_kind == "claim":
        for page in store.list_pages():
            if target_id in page.claims:
                steps.append(
                    {"kind": "page", "id": page.id, "unlink_claims": [target_id]}
                )
        for goal in store.list_goals():
            if target_id in goal.claims:
                steps.append(
                    {"kind": "goal", "id": goal.id, "unlink_claims": [target_id]}
                )
        steps.extend(_relation_cascade_steps(store, target_id))
        for claim in store.list_claims():
            if claim.id == target_id:
                continue
            step: dict[str, Any] = {"kind": "claim", "id": claim.id}
            if target_id in claim.supersedes:
                step["unlink_supersedes"] = [target_id]
            if claim.superseded_by == target_id:
                step["clear_superseded_by"] = True
            if target_id in claim.contradicts:
                step["unlink_contradicts"] = [target_id]
            if len(step) > 2:
                steps.append(step)
    elif target_kind == "page":
        steps.extend(_relation_cascade_steps(store, target_id))
    elif target_kind == "entity":
        for claim in store.list_claims():
            if target_id in claim.entities:
                steps.append(
                    {"kind": "claim", "id": claim.id, "unlink_entities": [target_id]}
                )
        for page in store.list_pages():
            if target_id in page.entities:
                steps.append(
                    {"kind": "page", "id": page.id, "unlink_entities": [target_id]}
                )
        for goal in store.list_goals():
            if target_id in goal.entities:
                steps.append(
                    {"kind": "goal", "id": goal.id, "unlink_entities": [target_id]}
                )
        steps.extend(_relation_cascade_steps(store, target_id))
    return steps


def _apply_cascade_page(
    store: KBStore, step: dict[str, Any], step_id: str, *, actor: str
) -> bool:
    try:
        page = store.get_page(step_id)
    except ArtifactNotFoundError:
        return False
    claims = [c for c in step.get("unlink_claims") or [] if c in page.claims]
    entities = [e for e in step.get("unlink_entities") or [] if e in page.entities]
    if not claims and not entities:
        return False
    page.claims = [c for c in page.claims if c not in claims]
    page.entities = [e for e in page.entities if e not in entities]
    if claims:
        # frontmatter and the inline [claim: …] markers both, or the body
        # keeps rendering a citation whose claim no longer exists.
        page.body = strip_claim_markers(page.body, claims)
    page.updated_at = datetime.now(UTC)
    store.update_page(page)
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_page(
            conn, id=page.id, title=page.title, body=page.body,
            type=page.type, tags=page.tags,
        )
    audit.log_event(
        store.kb_dir, event="page.cascade_unlink", actor=actor,
        object_ids=[page.id], data={"claims": claims, "entities": entities},
        reversible=False,
    )
    return True


def _apply_cascade_claim(
    store: KBStore, step: dict[str, Any], step_id: str, *, actor: str
) -> bool:
    try:
        claim = store.get_claim(step_id)
    except ArtifactNotFoundError:
        return False
    supersedes = [c for c in step.get("unlink_supersedes") or [] if c in claim.supersedes]
    contradicts = [
        c for c in step.get("unlink_contradicts") or [] if c in claim.contradicts
    ]
    entities = [e for e in step.get("unlink_entities") or [] if e in claim.entities]
    clear_superseded_by = (
        bool(step.get("clear_superseded_by")) and claim.superseded_by is not None
    )
    if not (supersedes or contradicts or entities or clear_superseded_by):
        return False
    claim.supersedes = [c for c in claim.supersedes if c not in supersedes]
    claim.contradicts = [c for c in claim.contradicts if c not in contradicts]
    claim.entities = [e for e in claim.entities if e not in entities]
    if clear_superseded_by:
        claim.superseded_by = None
    claim.updated_at = datetime.now(UTC)
    store.update_claim(claim)
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_claim(
            conn, id=claim.id, text=claim.text,
            type=claim.type.value, status=claim.status.value, tags=claim.tags,
        )
    audit.log_event(
        store.kb_dir, event="claim.cascade_unlink", actor=actor,
        object_ids=[claim.id],
        data={
            "supersedes": supersedes, "contradicts": contradicts,
            "entities": entities, "superseded_by_cleared": clear_superseded_by,
        },
        reversible=False,
    )
    return True


def _apply_cascade_goal(
    store: KBStore, step: dict[str, Any], step_id: str, *, actor: str
) -> bool:
    # Lazy import: lifecycle imports strip_claim_markers from this module.
    from . import lifecycle as life

    return (
        life.cascade_unlink_goal_refs(
            store,
            step_id,
            unlink_claims=list(step.get("unlink_claims") or []),
            unlink_entities=list(step.get("unlink_entities") or []),
            actor=actor,
        )
        is not None
    )


def _apply_cascade(
    store: KBStore, steps: list[dict[str, Any]], *, actor: str
) -> list[str]:
    """Apply an approved cascade's referrer edits. Returns the ids changed.

    Runs *before* the target is deleted, so the approve-time `referenced_by`
    gate below finds nothing and the delete proceeds — the gate is satisfied,
    never bypassed. Every step is idempotent: a referrer that was already
    edited or removed between propose and approve is skipped rather than
    fatal, which is what makes a crash-retry of approve() safe.
    """
    changed: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = str(step.get("kind", ""))
        step_id = str(step.get("id", ""))
        if not step_id:
            continue
        if kind == "relation":
            try:
                store.delete_relation(step_id)
            except ArtifactNotFoundError:
                continue
            with index_db.open_db(store.kb_dir) as conn:
                index_db.deindex(conn, kind="relation", id=step_id)
            audit.log_event(
                store.kb_dir, event="relation.delete", actor=actor,
                object_ids=[step_id], data={"cascade": True}, reversible=False,
            )
        elif kind == "page":
            if not _apply_cascade_page(store, step, step_id, actor=actor):
                continue
        elif kind == "claim":
            if not _apply_cascade_claim(store, step, step_id, actor=actor):
                continue
        elif kind == "goal":
            if not _apply_cascade_goal(store, step, step_id, actor=actor):
                continue
        else:
            continue
        changed.append(step_id)
    return changed


def _reconstruct_deleted(
    target_kind: str, snapshot: dict[str, Any]
) -> Claim | Page | Entity | Relation | Goal:
    """Rebuild a typed model from a delete proposal's snapshot.

    Used only on the idempotent path (artifact already gone) so the approve
    surfaces still receive a `{kind, id}` result.
    """
    if target_kind == "claim":
        return Claim(**snapshot)
    if target_kind == "page":
        return Page(**snapshot)
    if target_kind == "entity":
        return Entity(**snapshot)
    return Relation(**snapshot)


def _approve_delete(
    store: KBStore, proposal: Proposal, *, approved_by: str
) -> Claim | Page | Entity | Relation | Goal:
    """Execute an approved DELETE proposal: remove the artifact + index rows.

    Re-checks references at approve time (they may have appeared since the
    proposal was filed). Idempotent: if the artifact is already gone, finalize
    the proposal without erroring.
    """
    payload = proposal.payload
    target_kind = str(payload["target_kind"])
    target_id = str(payload["id"])
    snapshot = dict(payload.get("snapshot") or {})
    getter = getattr(store, _DELETE_GETTERS[target_kind])
    try:
        artifact = getter(target_id)
    except ArtifactNotFoundError:
        # Idempotent already-gone path (crash-retry between the file unlink and
        # move_proposal_to_decided). The file is gone but the derived index rows
        # may not be — a crash between deleter() and deindex() below would leave
        # stale fts/embedding/prov rows and keep the deleted artifact searchable.
        # deindex is a no-op when the rows are already absent, so run it here too
        # to converge the index. The per-kind {kind}.delete audit event is
        # intentionally NOT re-emitted on this path: the snapshot is preserved in
        # the decided/ proposal, the shared approve() tail still records
        # proposal.delete.approve, and re-emitting would double-log if the crash
        # landed after the original audit call.
        with index_db.open_db(store.kb_dir) as conn:
            index_db.deindex(conn, kind=target_kind, id=target_id)
        return _reconstruct_deleted(target_kind, snapshot)
    cascaded: list[str] = []
    if "cascade" in payload:
        # Re-derived here rather than replayed from the payload, for the same
        # reason refs are re-checked: the KB may have moved since the proposal
        # was filed. A referrer added after propose time is still unlinked; one
        # removed since is simply absent from the new plan. The payload's copy
        # stays as the reviewer-visible record of what they approved.
        cascaded = _apply_cascade(
            store, cascade_plan(store, target_kind, target_id), actor=approved_by
        )
    refs = referenced_by(store, target_kind, target_id)
    if refs:
        raise ProposalError(
            f"cannot delete {target_kind} {target_id}: still referenced by "
            + ", ".join(refs)
        )
    deleter = getattr(store, f"delete_{target_kind}")
    deleter(target_id)
    with index_db.open_db(store.kb_dir) as conn:
        index_db.deindex(conn, kind=target_kind, id=target_id)
    data: dict[str, Any] = {"snapshot": snapshot}
    if cascaded:
        data["cascaded"] = cascaded
    audit.log_event(
        store.kb_dir, event=f"{target_kind}.delete", actor=approved_by,
        object_ids=[target_id], data=data, reversible=False,
    )
    return artifact


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
    # rstrip *after* truncating: cutting at 60 chars can land mid-word and
    # leave a trailing dash, which makes for an ugly id and a citation that
    # readers (and looser id matchers) trip over.
    return slug[:60].rstrip("-") or "untitled"
