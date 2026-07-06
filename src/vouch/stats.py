"""KB observability — `vouch stats` / `kb.stats`.

Read-only aggregates for multi-agent review workflows: pending queue by
agent, decision rates over a time window, and citation coverage.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from . import audit, health
from .models import Proposal, ProposalStatus
from .proposals import EXPIRE_REASON
from .storage import KBStore, _yaml_load


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _cutoff(*, since_days: int | None) -> datetime | None:
    if since_days is None:
        return None
    return datetime.now(UTC) - timedelta(days=since_days)


def _in_window(dt: datetime | None, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    if dt is None:
        return False
    return _utc(dt) >= cutoff


def _decision_bucket(proposal: Proposal) -> str:
    if proposal.status == ProposalStatus.APPROVED:
        return "approved"
    if proposal.decision_reason == EXPIRE_REASON:
        return "expired"
    return "rejected"


def _list_decided(store: KBStore) -> list[Proposal]:
    ddir = store.kb_dir / "decided"
    if not ddir.is_dir():
        return []
    out: list[Proposal] = []
    for path in sorted(ddir.glob("*.yaml")):
        out.append(Proposal.model_validate(_yaml_load(path.read_text(encoding="utf-8"))))
    return out


def _audit_decision_kind(event: str) -> str | None:
    if event == "proposal.expire":
        return "expired"
    if event.endswith(".approve"):
        return "approved"
    if event.endswith(".reject"):
        return "rejected"
    return None


def _audit_review_totals(
    kb_dir: Any, *, cutoff: datetime | None,
) -> dict[str, int]:
    totals = {"approved": 0, "rejected": 0, "expired": 0}
    for ev in audit.read_events(kb_dir):
        kind = _audit_decision_kind(ev.event)
        if kind is None:
            continue
        if not _in_window(ev.created_at, cutoff):
            continue
        totals[kind] += 1
    return totals


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def citation_summary(store: KBStore) -> dict[str, Any]:
    claims, findings = health._load_claims_for_lint(store)
    sources_present = {s.id for s in store.list_sources()}
    evidence_present = {e.id for e in store.list_evidence()}

    invalid_claim = sum(
        1 for f in findings if f.code == "invalid_claim" and f.object_ids
    )

    broken_claim_ids: set[str] = set()
    with_valid = 0
    for c in claims:
        if not c.evidence:
            continue
        refs_ok = all(
            ref in sources_present or ref in evidence_present for ref in c.evidence
        )
        if not refs_ok:
            broken_claim_ids.add(c.id)
            continue
        with_valid += 1

    loadable = len(claims)
    total = loadable + invalid_claim
    return {
        "claims_total": total,
        "claims_loadable": loadable,
        "claims_with_valid_citation": with_valid,
        "invalid_claim": invalid_claim,
        "broken_citation": len(broken_claim_ids),
        "coverage_rate": _rate(with_valid, total),
    }


def pending_summary(store: KBStore) -> dict[str, Any]:
    pending = store.list_proposals(ProposalStatus.PENDING)
    by_agent: dict[str, int] = {}
    ages_days: list[float] = []
    oldest: tuple[float, str] | None = None
    now = datetime.now(UTC)

    for pr in pending:
        agent = pr.proposed_by or "unknown"
        by_agent[agent] = by_agent.get(agent, 0) + 1
        age = (now - _utc(pr.proposed_at)).total_seconds() / 86400.0
        ages_days.append(age)
        if oldest is None or age > oldest[0]:
            oldest = (age, pr.id)

    median_age = round(statistics.median(ages_days), 2) if ages_days else None
    max_age = round(max(ages_days), 2) if ages_days else None
    return {
        "total": len(pending),
        "by_agent": dict(sorted(by_agent.items(), key=lambda kv: (-kv[1], kv[0]))),
        "age_days": {
            "median": median_age,
            "max": max_age,
            "oldest_id": oldest[1] if oldest else None,
        },
    }


def review_summary(store: KBStore, *, since_days: int | None) -> dict[str, Any]:
    cutoff = _cutoff(since_days=since_days)
    totals = {"approved": 0, "rejected": 0, "expired": 0}
    by_agent: dict[str, dict[str, int]] = {}

    def bump(agent: str, bucket: str) -> None:
        row = by_agent.setdefault(
            agent,
            {"pending": 0, "approved": 0, "rejected": 0, "expired": 0},
        )
        row[bucket] += 1

    for pr in store.list_proposals(ProposalStatus.PENDING):
        bump(pr.proposed_by or "unknown", "pending")

    for pr in _list_decided(store):
        if not _in_window(pr.decided_at, cutoff):
            continue
        bucket = _decision_bucket(pr)
        totals[bucket] += 1
        bump(pr.proposed_by or "unknown", bucket)

    decided = totals["approved"] + totals["rejected"] + totals["expired"]
    audit_totals = _audit_review_totals(store.kb_dir, cutoff=cutoff)

    return {
        "window_days": since_days,
        "decided_in_window": decided,
        "approved": totals["approved"],
        "rejected": totals["rejected"],
        "expired": totals["expired"],
        "approval_rate": _rate(totals["approved"], decided),
        "audit_totals": audit_totals,
        "by_agent": {
            agent: dict(sorted(counts.items()))
            for agent, counts in sorted(by_agent.items())
        },
    }


def collect_stats(store: KBStore, *, since_days: int | None = 30) -> dict[str, Any]:
    """Aggregate observability metrics for the KB at ``store``."""
    return {
        "kb_dir": str(store.kb_dir),
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": health.status(store),
        "pending": pending_summary(store),
        "review": review_summary(store, since_days=since_days),
        "citations": citation_summary(store),
    }
