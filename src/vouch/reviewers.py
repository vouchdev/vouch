"""Read-only reviewer-throughput report: who works the gate, and how.

`vouch stats` buckets the review queue by the *proposing* agent (`proposed_by`)
and `vouch digest` lists recent decisions one at a time. Neither aggregates by
the human at the gate — the `decided_by` actor who actually approves or rejects.
This viewport does: per reviewer, how many proposals they approved versus
rejected, their approval rate, and how long proposals waited before they decided
(turnaround, from `proposed_at` to `decided_at`).

An auto-expiry is not a review decision — a proposal that ages out is closed by
the `vouch-expire` actor, not a human — so expiries are excluded from every
reviewer's counts and reported once, separately, as `expired_total`.

Strictly a viewport: it composes `store.list_proposals`, writes nothing, logs no
audit event, and never touches a proposal — there is nothing here for the review
gate to gate.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import ProposalStatus
from .proposals import EXPIRE_REASON
from .storage import KBStore

DEFAULT_SINCE_SPEC = "30d"

_UNKNOWN_REVIEWER = "(unknown)"


@dataclass(frozen=True)
class ReviewerRow:
    reviewer: str
    decisions: int
    approved: int
    rejected: int
    approval_rate: float | None
    turnaround_hours_median: float | None
    turnaround_hours_max: float | None


@dataclass(frozen=True)
class ReviewerReport:
    """Stable `to_dict()` schema — the `--format json` contract."""

    generated_at: str
    since: str | None
    reviewers_total: int
    decisions_total: int
    expired_total: int
    reviewers: list[ReviewerRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@dataclass
class _Acc:
    approved: int = 0
    rejected: int = 0
    turnarounds: list[float] = field(default_factory=list)


def build(
    store: KBStore,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> ReviewerReport:
    """Compose the reviewer-throughput report. Read-only by construction."""
    now = _as_utc(now) or datetime.now(UTC)
    since = _as_utc(since)

    accs: dict[str, _Acc] = {}
    decisions_total = 0
    expired_total = 0

    decided = [
        *store.list_proposals(ProposalStatus.APPROVED),
        *store.list_proposals(ProposalStatus.REJECTED),
    ]
    for p in decided:
        decided_at = _as_utc(p.decided_at)
        if decided_at is None:
            # An APPROVED/REJECTED proposal with no decided_at can't be placed
            # in the window or attributed in time — skip rather than guess.
            continue
        if since is not None and decided_at < since:
            continue
        if p.decision_reason == EXPIRE_REASON:
            expired_total += 1
            continue

        decisions_total += 1
        acc = accs.setdefault(p.decided_by or _UNKNOWN_REVIEWER, _Acc())
        if p.status == ProposalStatus.APPROVED:
            acc.approved += 1
        else:
            acc.rejected += 1

        proposed_at = _as_utc(p.proposed_at)
        if proposed_at is not None and decided_at >= proposed_at:
            acc.turnarounds.append((decided_at - proposed_at).total_seconds() / 3600.0)

    rows = [
        ReviewerRow(
            reviewer=reviewer,
            decisions=acc.approved + acc.rejected,
            approved=acc.approved,
            rejected=acc.rejected,
            approval_rate=(
                round(acc.approved / (acc.approved + acc.rejected), 4)
                if (acc.approved + acc.rejected)
                else None
            ),
            turnaround_hours_median=(
                round(statistics.median(acc.turnarounds), 2) if acc.turnarounds else None
            ),
            turnaround_hours_max=(
                round(max(acc.turnarounds), 2) if acc.turnarounds else None
            ),
        )
        for reviewer, acc in accs.items()
    ]
    rows.sort(key=lambda r: (-r.decisions, r.reviewer))

    return ReviewerReport(
        generated_at=now.isoformat(timespec="seconds"),
        since=since.isoformat(timespec="seconds") if since else None,
        reviewers_total=len(rows),
        decisions_total=decisions_total,
        expired_total=expired_total,
        reviewers=rows,
    )


def _rate(rate: float | None) -> str:
    return f"{rate:.0%}" if rate is not None else "n/a"


def _hours(value: float | None) -> str:
    return f"{value:g}h" if value is not None else "n/a"


def render_text(report: ReviewerReport) -> str:
    lines = [
        f"reviewer throughput @ {report.generated_at}  "
        f"(window since: {report.since or 'all'})",
        f"reviewers: {report.reviewers_total}  decisions: {report.decisions_total}  "
        f"expired (excluded): {report.expired_total}",
        "",
    ]
    for r in report.reviewers:
        lines.append(
            f"  {r.reviewer}  {r.decisions} decision(s)  "
            f"{r.approved} approved / {r.rejected} rejected  "
            f"approval {_rate(r.approval_rate)}  "
            f"turnaround med {_hours(r.turnaround_hours_median)} / "
            f"max {_hours(r.turnaround_hours_max)}"
        )
    if not report.reviewers:
        lines.append("  none")
    return "\n".join(lines)


def render_markdown(report: ReviewerReport) -> str:
    lines = [
        f"# reviewer throughput — {report.generated_at}",
        "",
        f"window since: {report.since or 'all'} · reviewers: {report.reviewers_total} · "
        f"decisions: {report.decisions_total} · expired (excluded): {report.expired_total}",
        "",
    ]
    lines += [
        f"- **{r.reviewer}** — {r.decisions} decision(s), {r.approved} approved / "
        f"{r.rejected} rejected, approval {_rate(r.approval_rate)}, "
        f"turnaround med {_hours(r.turnaround_hours_median)} / "
        f"max {_hours(r.turnaround_hours_max)}"
        for r in report.reviewers
    ] or ["- none"]
    return "\n".join(lines)
