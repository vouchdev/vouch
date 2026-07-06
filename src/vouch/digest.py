"""Read-only reviewer briefing: what needs the human at the gate right now.

An operator returning to a KB after a day otherwise reconstructs "what needs
me" from several commands: `vouch pending` for the backlog, `vouch metrics`
for rates, a walk through `decided/` for what happened, `vouch pages` for
due followups. This folds them into one glance, as named oldest-first lists
rather than aggregate gauges.

Strictly a viewport: it composes `store.list_proposals`, `store.list_claims`,
`store.list_pages` and `metrics.compute`, writes nothing, logs no audit
event, and never touches a proposal — so there is nothing here for the
review gate to gate. Run it from cron and pipe `--format markdown` wherever
the team reads its mornings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .metrics import DEFAULT_STALE_DAYS, compute
from .models import ClaimStatus, ProposalStatus
from .page_filters import filter_pages
from .storage import KBStore

DEFAULT_LIMIT = 10
DEFAULT_SINCE_SPEC = "7d"

# Followup states that no longer need attention. Everything else is open by
# definition — an unknown status should surface, not hide.
_CLOSED_FOLLOWUP_STATUSES = {"done", "dropped"}

_RETIRED_CLAIM_STATUSES = {
    ClaimStatus.SUPERSEDED,
    ClaimStatus.ARCHIVED,
    ClaimStatus.REDACTED,
}


@dataclass(frozen=True)
class PendingRow:
    id: str
    kind: str
    proposed_by: str
    proposed_at: str
    age_days: int


@dataclass(frozen=True)
class DecisionRow:
    id: str
    kind: str
    decision: str
    decided_by: str
    decided_at: str
    title: str


@dataclass(frozen=True)
class StaleRow:
    id: str
    text: str
    anchor: str
    age_days: int


@dataclass(frozen=True)
class FollowupRow:
    id: str
    title: str
    due_at: str
    owner: str | None
    followup_status: str


@dataclass(frozen=True)
class Digest:
    """Stable `to_dict()` schema — the `--format json` contract."""

    generated_at: str
    since: str | None
    stale_after_days: int
    limit: int
    pending_total: int
    pending: list[PendingRow] = field(default_factory=list)
    decisions: list[DecisionRow] = field(default_factory=list)
    stale_claims: list[StaleRow] = field(default_factory=list)
    stale_total: int = 0
    followups_due: list[FollowupRow] = field(default_factory=list)
    citation_coverage: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _payload_title(payload: dict[str, Any]) -> str:
    for key in ("title", "text", "name", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value if len(value) <= 72 else value[:69] + "..."
    return "(untitled)"


def build(
    store: KBStore,
    *,
    since: datetime | None = None,
    stale_after_days: int = DEFAULT_STALE_DAYS,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> Digest:
    """Compose the briefing. Read-only by construction."""
    now = _as_utc(now) or datetime.now(UTC)
    since = _as_utc(since)

    pending_all = sorted(
        store.list_proposals(ProposalStatus.PENDING),
        key=lambda p: _as_utc(p.proposed_at) or now,
    )
    pending_rows = [
        PendingRow(
            id=p.id,
            kind=p.kind.value,
            proposed_by=p.proposed_by,
            proposed_at=(_as_utc(p.proposed_at) or now).isoformat(timespec="seconds"),
            age_days=max(0, (now - (_as_utc(p.proposed_at) or now)).days),
        )
        for p in pending_all[:limit]
    ]

    decided = [
        p
        for status in (ProposalStatus.APPROVED, ProposalStatus.REJECTED)
        for p in store.list_proposals(status)
        if p.decided_at is not None
        and (since is None or (_as_utc(p.decided_at) or now) >= since)
    ]
    decided.sort(key=lambda p: _as_utc(p.decided_at) or now, reverse=True)
    decision_rows = [
        DecisionRow(
            id=p.id,
            kind=p.kind.value,
            decision=p.status.value,
            decided_by=p.decided_by or "(unknown)",
            decided_at=(_as_utc(p.decided_at) or now).isoformat(timespec="seconds"),
            title=_payload_title(p.payload),
        )
        for p in decided[:limit]
    ]

    # Same freshness anchor and thresholds as `vouch metrics` / `vouch lint`,
    # rendered as the oldest-first list behind the count they report.
    threshold = timedelta(days=stale_after_days)
    stale: list[tuple[datetime, StaleRow]] = []
    for c in store.list_claims():
        if c.status in _RETIRED_CLAIM_STATUSES:
            continue
        anchor = _as_utc(c.last_confirmed_at or c.updated_at or c.created_at)
        if anchor is not None and (now - anchor) > threshold:
            text = c.text if len(c.text) <= 96 else c.text[:93] + "..."
            stale.append(
                (
                    anchor,
                    StaleRow(
                        id=c.id,
                        text=text,
                        anchor=anchor.isoformat(timespec="seconds"),
                        age_days=(now - anchor).days,
                    ),
                )
            )
    stale.sort(key=lambda pair: pair[0])
    stale_rows = [row for _, row in stale[:limit]]

    due_pages = filter_pages(
        store.list_pages(),
        kind="followup",
        before={"due_at": now.date().isoformat()},
    )
    followup_rows = sorted(
        (
            FollowupRow(
                id=p.id,
                title=p.title,
                due_at=str(p.metadata.get("due_at", "")),
                owner=(str(p.metadata["owner"]) if p.metadata.get("owner") else None),
                followup_status=str(p.metadata.get("followup_status", "")),
            )
            for p in due_pages
            if str(p.metadata.get("followup_status", "")) not in _CLOSED_FOLLOWUP_STATUSES
        ),
        key=lambda r: r.due_at,
    )

    m = compute(store, since=since, stale_after_days=stale_after_days, now=now)

    return Digest(
        generated_at=now.isoformat(timespec="seconds"),
        since=since.isoformat(timespec="seconds") if since else None,
        stale_after_days=stale_after_days,
        limit=limit,
        pending_total=len(pending_all),
        pending=pending_rows,
        decisions=decision_rows,
        stale_claims=stale_rows,
        stale_total=m.stale_claims,
        followups_due=followup_rows,
        citation_coverage=m.citation_coverage,
    )


def render_text(d: Digest) -> str:
    lines = [f"digest @ {d.generated_at}  (window since: {d.since or 'all'})", ""]
    lines.append(f"pending awaiting review: {d.pending_total}")
    for pr in d.pending:
        lines.append(f"  {pr.id}  [{pr.kind}]  by {pr.proposed_by}  {pr.age_days}d old")
    if d.pending_total > len(d.pending):
        lines.append(f"  ... and {d.pending_total - len(d.pending)} more (vouch pending)")
    lines.append("")
    lines.append(f"recent decisions: {len(d.decisions)}")
    for dr in d.decisions:
        lines.append(f"  {dr.decision:<8} {dr.id}  [{dr.kind}]  {dr.title}  by {dr.decided_by}")
    lines.append("")
    lines.append(f"stale claims (> {d.stale_after_days}d): {d.stale_total}")
    for sr in d.stale_claims:
        lines.append(f"  {sr.id}  {sr.age_days}d  {sr.text}")
    lines.append("")
    lines.append(f"followups due: {len(d.followups_due)}")
    for fr in d.followups_due:
        owner = f"  owner: {fr.owner}" if fr.owner else ""
        lines.append(f"  {fr.due_at}  {fr.id}  {fr.title}{owner}")
    if d.citation_coverage is not None:
        lines.append("")
        lines.append(f"citation coverage: {d.citation_coverage:.0%}")
    return "\n".join(lines)


def render_markdown(d: Digest) -> str:
    lines = [f"# kb digest — {d.generated_at}", ""]
    lines.append(f"## pending awaiting review ({d.pending_total})")
    lines += [
        f"- `{r.id}` [{r.kind}] by {r.proposed_by}, {r.age_days}d old" for r in d.pending
    ] or ["- none"]
    lines.append("")
    lines.append(f"## recent decisions ({len(d.decisions)})")
    lines += [
        f"- **{r.decision}** `{r.id}` [{r.kind}] {r.title} — {r.decided_by}"
        for r in d.decisions
    ] or ["- none"]
    lines.append("")
    lines.append(f"## stale claims > {d.stale_after_days}d ({d.stale_total})")
    lines += [f"- `{r.id}` {r.age_days}d: {r.text}" for r in d.stale_claims] or ["- none"]
    lines.append("")
    lines.append(f"## followups due ({len(d.followups_due)})")
    lines += [
        f"- {r.due_at} `{r.id}` {r.title}" + (f" (owner: {r.owner})" if r.owner else "")
        for r in d.followups_due
    ] or ["- none"]
    if d.citation_coverage is not None:
        lines.append("")
        lines.append(f"citation coverage: {d.citation_coverage:.0%}")
    return "\n".join(lines)
