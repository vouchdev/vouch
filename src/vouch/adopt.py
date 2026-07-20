"""Drain personal-KB fallback captures into a project's own KB.

A machine-wide install plus an opted-in personal KB means sessions in
folders without a project ``.vouch/`` still capture — into the personal
catch-all, with the folder they came from stamped on each captured source
(``metadata.origin_path``). ``vouch adopt``, run inside a project that now
HAS a KB, finds those strays and moves the knowledge home.

Through the gate, never around it: sources are copied byte-identically
(content addressing keeps their ids stable across KBs), each live personal
claim citing them is RE-PROPOSED against the copied source, its byte-offset
receipt re-verifies mechanically, and the project KB's own review config
decides durability exactly as it would for a fresh capture — auto-approve
on receipt where enabled, pending for a human otherwise. Claims without a
verifying receipt are proposed citing the copied source and always wait for
a human.

The personal copies stay put by default (audit history is append-only and
the personal KB's history of "what I learned where" has value of its own);
``retire=True`` archives the adopted claims there so they stop surfacing in
personal recall. Both KBs log a ``kb.adopt`` audit event carrying the other
side's id — the cross-KB attribution the instance identity exists for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import audit as audit_mod
from . import lifecycle
from . import proposals as proposals_mod
from .models import Claim, ClaimStatus, Evidence, ProposalStatus, Source
from .storage import ArtifactNotFoundError, KBStore

ADOPT_ACTOR = "vouch-adopt"

# Claim statuses that never travel: superseded/archived knowledge was
# retired on purpose, redacted knowledge must not propagate.
_DEAD_STATUSES = frozenset(
    {ClaimStatus.SUPERSEDED, ClaimStatus.ARCHIVED, ClaimStatus.REDACTED}
)


@dataclass
class AdoptReport:
    """What one adopt pass did (or, under dry_run, would do)."""

    origin: str
    from_kb: str | None
    to_kb: str | None
    dry_run: bool
    sources: list[str] = field(default_factory=list)
    claims_durable: list[str] = field(default_factory=list)
    claims_pending: list[str] = field(default_factory=list)
    claims_skipped: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "from_kb": self.from_kb,
            "to_kb": self.to_kb,
            "dry_run": self.dry_run,
            "sources": self.sources,
            "claims_durable": self.claims_durable,
            "claims_pending": self.claims_pending,
            "claims_skipped": self.claims_skipped,
            "retired": self.retired,
        }


def _origin_matches(origin_path: str, match_root: Path) -> bool:
    """True when the capture's recorded origin folder is match_root or below."""
    try:
        origin = Path(origin_path).resolve()
    except OSError:
        return False
    return origin == match_root or match_root in origin.parents


def find_adoptable_sources(personal: KBStore, match_root: Path) -> list[Source]:
    """Personal-KB sources captured in ``match_root`` (or a subfolder)."""
    root = match_root.resolve()
    out: list[Source] = []
    for src in personal.list_sources():
        origin_path = src.metadata.get("origin_path")
        if isinstance(origin_path, str) and origin_path and _origin_matches(
            origin_path, root
        ):
            out.append(src)
    return out


def _claims_citing(
    personal: KBStore, source_ids: set[str]
) -> list[tuple[Claim, Evidence | None]]:
    """Live personal claims citing any of ``source_ids``, with a receipt if any.

    A claim cites a source either directly (a bare source id in evidence) or
    through a receipt-carrying Evidence whose ``source_id`` points there. The
    first evidence with a verifiable quote wins as the receipt to re-propose
    with; a claim with only bare citations travels receipt-less (pending).
    """
    pairs: list[tuple[Claim, Evidence | None]] = []
    for claim in personal.list_claims():
        if claim.status in _DEAD_STATUSES:
            continue
        receipt: Evidence | None = None
        cited = False
        for eid in claim.evidence:
            if eid in source_ids:
                cited = True
                continue
            try:
                ev = personal.get_evidence(eid)
            except ArtifactNotFoundError:
                continue
            if ev.source_id in source_ids:
                cited = True
                if receipt is None and ev.quote:
                    receipt = ev
        if cited:
            pairs.append((claim, receipt))
    return pairs


def adopt(
    project: KBStore,
    personal: KBStore,
    *,
    match_root: Path,
    actor: str = ADOPT_ACTOR,
    retire: bool = False,
    dry_run: bool = False,
) -> AdoptReport:
    """One adopt pass: copy matching sources, re-propose their live claims.

    Idempotent: sources are content-addressed (a re-copy is a no-op), claims
    whose id is already durable in the project KB are skipped up front, and a
    re-proposal that decodes to an identical durable claim is mechanically
    rejected by the receipt resolver. ``dry_run`` reports without writing.
    """
    root = match_root.resolve()
    personal_identity = personal.identity()
    project_identity = project.identity()
    report = AdoptReport(
        origin=str(root),
        from_kb=personal_identity[0] if personal_identity else None,
        to_kb=project_identity[0] if project_identity else None,
        dry_run=dry_run,
    )
    sources = find_adoptable_sources(personal, root)
    if not sources:
        return report
    source_ids = {s.id for s in sources}
    pairs = _claims_citing(personal, source_ids)

    if dry_run:
        report.sources = sorted(
            sid for sid in source_ids if not _source_exists(project, sid)
        )
        for claim, receipt in pairs:
            if _already_durable(project, claim):
                report.claims_skipped.append(claim.id)
            elif receipt is not None:
                report.claims_durable.append(claim.id)  # candidate, gate decides
            else:
                report.claims_pending.append(claim.id)
        return report

    for src in sources:
        if _source_exists(project, src.id):
            continue  # content-addressed: already here from a prior pass
        content = personal.read_source_content(src.id)
        project.put_source(
            content,
            title=src.title,
            source_type=str(src.type),
            media_type=src.media_type,
            tags=_with_tag(src.tags, "adopted"),
            metadata={
                **src.metadata,
                "adopted_from": report.from_kb,
            },
            # The project's own stamp, not the personal KB's: from here on
            # this knowledge belongs to this project.
            scope=proposals_mod.default_scope(project),
        )
        report.sources.append(src.id)

    adopted_claim_ids: list[str] = []
    for claim, receipt in pairs:
        if _already_durable(project, claim):
            report.claims_skipped.append(claim.id)
            continue
        rationale = (
            f"adopted from personal KB {report.from_kb or '(no id)'} — "
            f"captured in {root}"
        )
        if receipt is not None and receipt.quote:
            result = proposals_mod.propose_quoted_claim(
                project,
                text=claim.text,
                source_id=receipt.source_id,
                quote=receipt.quote,
                proposed_by=actor,
                claim_type=str(claim.type),
                confidence=claim.confidence,
                tags=_with_tag(claim.tags, "adopted"),
                rationale=rationale,
                slug_hint=claim.id,
            )
            if result is None:
                # The quote no longer locates in the copied bytes — should be
                # impossible (same bytes), but never adopt what cannot verify.
                report.claims_skipped.append(claim.id)
                continue
            durable = proposals_mod.resolve_pending_receipt_claim(
                project,
                result.proposal,
                actor=actor,
                reason="adopted from personal KB (receipt re-verified)",
            )
            if durable is not None:
                report.claims_durable.append(durable.id)
                adopted_claim_ids.append(claim.id)
            else:
                try:
                    filed = project.get_proposal(result.proposal.id)
                except ArtifactNotFoundError:
                    filed = None
                if filed is not None and filed.status == ProposalStatus.PENDING:
                    report.claims_pending.append(claim.id)
                    adopted_claim_ids.append(claim.id)
                else:
                    # Rejected as a duplicate of an already-durable claim.
                    report.claims_skipped.append(claim.id)
        else:
            evidence = [eid for eid in claim.evidence if eid in source_ids]
            if not evidence:
                report.claims_skipped.append(claim.id)
                continue
            proposals_mod.propose_claim(
                project,
                text=claim.text,
                evidence=evidence,
                proposed_by=actor,
                claim_type=str(claim.type),
                confidence=claim.confidence,
                tags=_with_tag(claim.tags, "adopted"),
                rationale=rationale,
                slug_hint=claim.id,
            )
            report.claims_pending.append(claim.id)
            adopted_claim_ids.append(claim.id)

    if retire:
        for claim_id in adopted_claim_ids:
            try:
                lifecycle.archive(personal, claim_id=claim_id, actor=actor)
            except Exception:
                # Retiring is best-effort tidying of the personal KB; a claim
                # that cannot be archived must not fail the adoption.
                continue
            report.retired.append(claim_id)

    moved = bool(report.sources or report.claims_durable or report.claims_pending)
    if moved:
        data = {
            "origin": report.origin,
            "sources": len(report.sources),
            "claims_durable": len(report.claims_durable),
            "claims_pending": len(report.claims_pending),
            "retired": len(report.retired),
        }
        audit_mod.log_event(
            project.kb_dir,
            event="kb.adopt",
            actor=actor,
            data={**data, "direction": "in", "from_kb": report.from_kb},
        )
        audit_mod.log_event(
            personal.kb_dir,
            event="kb.adopt",
            actor=actor,
            data={**data, "direction": "out", "to_kb": report.to_kb},
        )
    return report


def _already_durable(project: KBStore, claim: Claim) -> bool:
    try:
        project.get_claim(claim.id)
    except ArtifactNotFoundError:
        return False
    return True


def _source_exists(project: KBStore, source_id: str) -> bool:
    try:
        project.get_source(source_id)
    except ArtifactNotFoundError:
        return False
    return True


def _with_tag(tags: list[str], tag: str) -> list[str]:
    return tags if tag in tags else [*tags, tag]
