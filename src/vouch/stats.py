"""KB observability — `vouch stats` / `kb.stats`.

Read-only aggregates for multi-agent review workflows: pending queue by
agent, decision rates over a time window, and citation coverage.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import audit, health
from .models import Proposal, ProposalStatus
from .proposals import EXPIRE_REASON
from .storage import KBStore, _yaml_load

if TYPE_CHECKING:
    from .scoping import ViewerContext


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


# Real-world UTC offsets span -12:00..+14:00; clamp so a bad client can't
# shift events into arbitrary buckets.
_MAX_TZ_OFFSET_MINUTES = 14 * 60


def _is_proposal_create(event: str) -> bool:
    return event.startswith("proposal.") and event.endswith(".create")


def _local_clock(tz: str | None, offset_minutes: int) -> Callable[[datetime], datetime]:
    """Viewer-local conversion: an IANA zone when resolvable (DST-correct),
    otherwise the fixed offset."""
    if tz:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return lambda dt: _utc(dt).astimezone(zone)
    shift = timedelta(minutes=offset_minutes)
    return lambda dt: _utc(dt) + shift


def collect_activity(
    store: KBStore,
    *,
    days: int = 365,
    tz_offset_minutes: int = 0,
    tz: str | None = None,
    viewer: ViewerContext | None = None,
) -> dict[str, Any]:
    """Bucket audit events for the console dashboard — `kb.activity`.

    One pass over the audit log: per-day totals (with proposal/decision
    breakdowns), an hour-of-week matrix, and actor/event histograms.
    ``days=0`` means all-time; otherwise the window is the last ``days``
    viewer-local calendar days including today, so the oldest day in a
    dashboard heatmap is never partially counted. ``tz`` (IANA name) wins
    over ``tz_offset_minutes`` for local bucketing. ``viewer`` applies the
    same scope filtering as `kb.audit`.
    """
    if days < 0:
        raise ValueError("days must be >= 0")
    window = None if days == 0 else days
    offset = max(-_MAX_TZ_OFFSET_MINUTES, min(_MAX_TZ_OFFSET_MINUTES, tz_offset_minutes))
    to_local = _local_clock(tz, offset)
    cutoff_day = (
        None
        if window is None
        else to_local(datetime.now(UTC)).date() - timedelta(days=window - 1)
    )

    by_day: dict[str, dict[str, int]] = {}
    # by_hour[weekday][hour], Monday = 0 — matches datetime.weekday().
    by_hour = [[0] * 24 for _ in range(7)]
    by_actor: dict[str, int] = {}
    by_event: dict[str, int] = {}
    total = 0

    for ev in audit.read_events(store.kb_dir, store=store, viewer=viewer):
        local = to_local(ev.created_at)
        if cutoff_day is not None and local.date() < cutoff_day:
            continue
        day = by_day.setdefault(
            local.date().isoformat(),
            {"total": 0, "proposals": 0, "decisions": 0},
        )
        day["total"] += 1
        if _is_proposal_create(ev.event):
            day["proposals"] += 1
        elif _audit_decision_kind(ev.event) is not None:
            day["decisions"] += 1
        by_hour[local.weekday()][local.hour] += 1
        by_actor[ev.actor] = by_actor.get(ev.actor, 0) + 1
        by_event[ev.event] = by_event.get(ev.event, 0) + 1
        total += 1

    days_seen = sorted(by_day)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": window,
        "tz_offset_minutes": offset,
        "viewer": {
            "project": viewer.project if viewer else None,
            "agent": viewer.agent if viewer else None,
        },
        "total_events": total,
        "active_days": len(by_day),
        "first_event_day": days_seen[0] if days_seen else None,
        "last_event_day": days_seen[-1] if days_seen else None,
        "by_day": {d: by_day[d] for d in days_seen},
        "by_hour": by_hour,
        "by_actor": dict(sorted(by_actor.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_event": dict(sorted(by_event.items(), key=lambda kv: (-kv[1], kv[0]))),
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
