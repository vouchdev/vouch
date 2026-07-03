"""``vouch digest`` — a read-only operator briefing (vouchdev/vouch#324).

``vouch metrics`` answers "is the review gate healthy?" with aggregate gauges
(rates, ratios, percentiles) built for graphing and alerting. ``vouch digest``
is its human-facing counterpart: named, oldest-first *lists* of what to act on
now, folded into one glance for an operator returning to a KB after a day or a
week — "n proposals awaiting review, what got decided while I was away, which
claims aged past the freshness threshold, and whether citation coverage moved".

Everything here is a **viewport** over sources that already exist on disk:

* ``store.list_proposals(PENDING)`` — the review backlog (same set as
  ``kb.list_pending``), oldest ``proposed_at`` first.
* ``.vouch/audit.log.jsonl`` via :func:`audit.read_events` — the authoritative
  approve/reject stream, titled from the ``decided/`` record.
* the claim artifacts — active claims whose freshness anchor is older than
  ``--stale-days``, listed rather than merely counted.
* ``metrics.compute`` — reused verbatim for the authoritative current citation
  coverage; the digest additionally computes coverage over the cohort of claims
  that already existed at the window start and reports the movement.

It is read-only by construction: it calls ``list_*`` / ``read_events`` and
formats. It never routes through ``propose_*`` / ``approve`` / ``reject``, so
the review gate is untouched — nothing here can create or edit knowledge, and
there is nothing to approve. No new on-disk state, no schema migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import metrics as metrics_mod
from .audit import read_events
from .metrics import _APPROVE_RE, _REJECT_RE, DEFAULT_STALE_DAYS, MetricsError
from .models import ClaimStatus, ProposalStatus
from .storage import ArtifactNotFoundError, KBStore

# A digest is inherently "since last look", so unlike ``vouch metrics`` (which
# defaults to all of history) it windows to a short recent span by default.
DEFAULT_SINCE = "7d"

# How many rows each list is capped at — a briefing stays a briefing.
DEFAULT_LIMIT = 10

# Bumped independently of ``metrics.SCHEMA_VERSION``; the ``to_dict`` shape is
# the contract a notification hook or standup script builds on.
SCHEMA_VERSION = 1

# Statuses that are *not* live corpus — mirrors ``metrics._fill_corpus_metrics``
# so "active" and "stale" mean exactly what ``vouch metrics`` / ``vouch lint``
# mean.
_RETIRED = frozenset(
    {ClaimStatus.SUPERSEDED, ClaimStatus.ARCHIVED, ClaimStatus.REDACTED}
)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _preview(payload: dict[str, Any]) -> str:
    """One-line summary of a proposal payload, matching ``vouch pending``."""
    text = payload.get("text") or payload.get("title") or payload.get("name") or payload.get("id")
    return str(text).strip() if text else "—"


# --- result containers ----------------------------------------------------


@dataclass(frozen=True)
class PendingItem:
    id: str
    kind: str
    proposed_by: str
    proposed_at: datetime
    age_seconds: float
    preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "proposed_by": self.proposed_by,
            "proposed_at": _iso(self.proposed_at),
            "age_seconds": self.age_seconds,
            "preview": self.preview,
        }


@dataclass(frozen=True)
class DecisionItem:
    proposal_id: str
    kind: str
    decision: str  # "approve" | "reject"
    actor: str
    decided_at: datetime
    title: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "decision": self.decision,
            "actor": self.actor,
            "decided_at": _iso(self.decided_at),
            "title": self.title,
        }


@dataclass(frozen=True)
class StaleItem:
    id: str
    anchor_at: datetime
    age_days: float
    preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "anchor_at": _iso(self.anchor_at),
            "age_days": self.age_days,
            "preview": self.preview,
        }


@dataclass
class Digest:
    """The full briefing. ``to_dict`` is the stable, documented schema."""

    since: datetime | None = None
    until: datetime | None = None
    generated_at: datetime | None = None
    stale_after_days: int = DEFAULT_STALE_DAYS
    limit: int = DEFAULT_LIMIT

    pending_total: int = 0
    pending: list[PendingItem] = field(default_factory=list)

    approvals: int = 0
    rejections: int = 0
    decisions: list[DecisionItem] = field(default_factory=list)

    stale_total: int = 0
    stale: list[StaleItem] = field(default_factory=list)

    citation_coverage_now: float | None = None
    citation_coverage_at_since: float | None = None
    citation_coverage_delta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "window": {
                "since": _iso(self.since),
                "until": _iso(self.until),
                "generated_at": _iso(self.generated_at),
            },
            "stale_after_days": self.stale_after_days,
            "limit": self.limit,
            "pending": {
                "total": self.pending_total,
                "items": [p.to_dict() for p in self.pending],
            },
            "decisions": {
                "approvals": self.approvals,
                "rejections": self.rejections,
                "total": self.approvals + self.rejections,
                "items": [d.to_dict() for d in self.decisions],
            },
            "stale": {
                "total": self.stale_total,
                "items": [s.to_dict() for s in self.stale],
            },
            "citation_coverage": {
                "now": self.citation_coverage_now,
                "at_since": self.citation_coverage_at_since,
                "delta": self.citation_coverage_delta,
            },
        }


# --- computation ----------------------------------------------------------


def compute_digest(
    store: KBStore,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    stale_after_days: int = DEFAULT_STALE_DAYS,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> Digest:
    """Build the :class:`Digest` briefing for ``store``.

    Read-only: streams the audit log once and loads the artifacts once. All
    predicates ("active", "stale", "cited") mirror ``metrics`` so the two
    surfaces never disagree.
    """
    now = _as_utc(now) or datetime.now(UTC)
    since = _as_utc(since)
    until = _as_utc(until)
    if since is not None and until is not None and since > until:
        raise MetricsError(
            f"--since ({since.isoformat()}) is after --until ({until.isoformat()})"
        )
    if stale_after_days < 0:
        raise MetricsError("--stale-days must be >= 0")
    if limit < 0:
        raise MetricsError("--limit must be >= 0")

    d = Digest(
        since=since,
        until=until,
        generated_at=now,
        stale_after_days=stale_after_days,
        limit=limit,
    )

    _fill_pending(store, d, now=now)
    _fill_decisions(store, d)
    _fill_stale(store, d, now=now, stale_after_days=stale_after_days)
    _fill_coverage(store, d, since=since, until=until, stale_after_days=stale_after_days, now=now)
    return d


def _fill_pending(store: KBStore, d: Digest, *, now: datetime) -> None:
    """Pending proposals, oldest ``proposed_at`` first — the actionable backlog
    behind ``metrics.pending_now``, rendered as a list rather than a count."""
    pending = store.list_proposals(ProposalStatus.PENDING)
    pending.sort(key=lambda pr: (_as_utc(pr.proposed_at) or now, pr.id))
    d.pending_total = len(pending)
    for pr in pending[: d.limit]:
        anchor = _as_utc(pr.proposed_at) or now
        d.pending.append(
            PendingItem(
                id=pr.id,
                kind=pr.kind.value,
                proposed_by=pr.proposed_by,
                proposed_at=anchor,
                age_seconds=max(0.0, (now - anchor).total_seconds()),
                preview=_preview(pr.payload),
            )
        )


def _fill_decisions(store: KBStore, d: Digest) -> None:
    """Approvals/rejections in the window from the authoritative audit stream,
    newest first, titled from the ``decided/`` record when it's still on disk."""
    hits: list[DecisionItem] = []
    approvals = 0
    rejections = 0
    for ev in read_events(store.kb_dir):
        ts = _as_utc(ev.created_at)
        if ts is None:
            continue
        if d.since is not None and ts < d.since:
            continue
        if d.until is not None and ts > d.until:
            continue

        am = _APPROVE_RE.match(ev.event)
        rm = None if am else _REJECT_RE.match(ev.event)
        m = am or rm
        if m is None:
            continue

        pid = ev.object_ids[0] if ev.object_ids else "—"
        hits.append(
            DecisionItem(
                proposal_id=pid,
                kind=m.group("kind"),
                decision="approve" if am else "reject",
                actor=ev.actor,
                decided_at=ts,
                title=_decision_title(store, pid),
            )
        )
        if am:
            approvals += 1
        else:
            rejections += 1

    d.approvals = approvals
    d.rejections = rejections
    # newest first; id tie-break for deterministic output on identical stamps.
    hits.sort(key=lambda h: (h.decided_at, h.proposal_id), reverse=True)
    d.decisions = hits[: d.limit]


def _decision_title(store: KBStore, pid: str) -> str:
    """Best-effort payload title for a decided proposal. The audit log is the
    authority for *that* a decision happened; the ``decided/`` record (which a
    KB may prune) only supplies a friendly title, so its absence is not fatal."""
    if pid == "—":
        return "—"
    try:
        return _preview(store.get_proposal(pid).payload)
    except (ArtifactNotFoundError, ValueError):
        return pid


def _fill_stale(
    store: KBStore, d: Digest, *, now: datetime, stale_after_days: int
) -> None:
    """Active claims whose freshness anchor is older than the threshold — the
    exact predicate in ``metrics._fill_corpus_metrics`` / ``vouch lint``, listed
    oldest first rather than merely counted."""
    threshold = timedelta(days=stale_after_days)
    rows: list[StaleItem] = []
    for c in store.list_claims():
        if c.status in _RETIRED:
            continue
        anchor = _as_utc(c.last_confirmed_at or c.updated_at or c.created_at)
        if anchor is None or (now - anchor) <= threshold:
            continue
        rows.append(
            StaleItem(
                id=c.id,
                anchor_at=anchor,
                age_days=(now - anchor).total_seconds() / 86400.0,
                preview=str(c.text).strip()[:120] or "—",
            )
        )
    rows.sort(key=lambda s: (s.anchor_at, s.id))  # oldest anchor first
    d.stale_total = len(rows)
    d.stale = rows[: d.limit]


def _fill_coverage(
    store: KBStore,
    d: Digest,
    *,
    since: datetime | None,
    until: datetime | None,
    stale_after_days: int,
    now: datetime,
) -> None:
    """Citation-coverage movement.

    ``now`` is the authoritative value, reused verbatim from ``metrics.compute``
    so the digest never disagrees with ``vouch metrics``. ``at_since`` is the
    same coverage restricted to the cohort of claims that already existed at the
    window start (``created_at <= since``), evaluated against the *current*
    source/evidence set — it shows how coverage moved as newer claims landed,
    not a historical snapshot (vouch does not retain past citation state). With
    an unbounded window (``since is None``) there is no baseline, so ``at_since``
    and ``delta`` are ``None``.
    """
    m = metrics_mod.compute(
        store,
        since=since,
        until=until,
        stale_after_days=stale_after_days,
        top_actors=0,
        now=now,
    )
    d.citation_coverage_now = m.citation_coverage

    if since is None:
        return

    resolvable = {s.id for s in store.list_sources()} | {e.id for e in store.list_evidence()}
    cohort_total = 0
    cohort_cited = 0
    for c in store.list_claims():
        created = _as_utc(c.created_at)
        if created is None or created > since:
            continue
        cohort_total += 1
        refs = list(c.evidence)
        if refs and all(r in resolvable for r in refs):
            cohort_cited += 1

    if cohort_total:
        d.citation_coverage_at_since = cohort_cited / cohort_total
        if d.citation_coverage_now is not None:
            d.citation_coverage_delta = d.citation_coverage_now - d.citation_coverage_at_since


# --- rendering ------------------------------------------------------------


def _fmt_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def _fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _fmt_delta(x: float | None) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x * 100:.1f}pp"


def _window_label(d: Digest) -> str:
    window = "all history" if d.since is None else f"since {d.since.isoformat()}"
    if d.until is not None:
        window += f" until {d.until.isoformat()}"
    return window


def render_text(d: Digest) -> str:
    """Human briefing (the default). Lists are already capped by ``limit``."""
    out: list[str] = []
    out.append(f"vouch digest  ({_window_label(d)})")
    out.append("")

    out.append(f"  pending review  ({d.pending_total})")
    if d.pending:
        for p in d.pending:
            out.append(f"    {p.id}  [{p.kind}]  by {p.proposed_by}  {_fmt_age(p.age_seconds)} old")
            out.append(f"      {p.preview[:100]}")
    else:
        out.append("    (nothing awaiting review)")
    if d.pending_total > len(d.pending):
        out.append(f"    … and {d.pending_total - len(d.pending)} more")
    out.append("")

    out.append(f"  decided in window  ({d.approvals} approved / {d.rejections} rejected)")
    if d.decisions:
        for de in d.decisions:
            verb = "approved" if de.decision == "approve" else "rejected"
            out.append(f"    {verb}  {de.proposal_id}  [{de.kind}]  by {de.actor}")
            out.append(f"      {de.title[:100]}")
    else:
        out.append("    (no decisions in window)")
    out.append("")

    out.append(f"  stale claims  ({d.stale_total} past {d.stale_after_days}d)")
    if d.stale:
        for s in d.stale:
            out.append(f"    {s.id}  {s.age_days:.0f}d old")
            out.append(f"      {s.preview[:100]}")
    else:
        out.append("    (nothing stale)")
    if d.stale_total > len(d.stale):
        out.append(f"    … and {d.stale_total - len(d.stale)} more")
    out.append("")

    out.append("  citation coverage")
    out.append(
        f"    now {_fmt_pct(d.citation_coverage_now)}  "
        f"(at window start {_fmt_pct(d.citation_coverage_at_since)}, "
        f"moved {_fmt_delta(d.citation_coverage_delta)})"
    )
    return "\n".join(out) + "\n"


def render_markdown(d: Digest) -> str:
    """A markdown block suitable for pasting into a notification or standup."""
    out: list[str] = []
    out.append(f"## vouch digest — {_window_label(d)}")
    out.append("")

    out.append(f"**Pending review** ({d.pending_total})")
    if d.pending:
        for p in d.pending:
            out.append(
                f"- `{p.id}` [{p.kind}] by {p.proposed_by}, "
                f"{_fmt_age(p.age_seconds)} old — {p.preview[:100]}"
            )
    else:
        out.append("- _nothing awaiting review_")
    if d.pending_total > len(d.pending):
        out.append(f"- _…and {d.pending_total - len(d.pending)} more_")
    out.append("")

    out.append(f"**Decided in window** ({d.approvals} approved / {d.rejections} rejected)")
    if d.decisions:
        for de in d.decisions:
            verb = "approved" if de.decision == "approve" else "rejected"
            out.append(f"- {verb} `{de.proposal_id}` [{de.kind}] by {de.actor} — {de.title[:100]}")
    else:
        out.append("- _no decisions in window_")
    out.append("")

    out.append(f"**Stale claims** ({d.stale_total} past {d.stale_after_days}d)")
    if d.stale:
        for s in d.stale:
            out.append(f"- `{s.id}` {s.age_days:.0f}d old — {s.preview[:100]}")
    else:
        out.append("- _nothing stale_")
    if d.stale_total > len(d.stale):
        out.append(f"- _…and {d.stale_total - len(d.stale)} more_")
    out.append("")

    out.append(
        f"**Citation coverage** — now {_fmt_pct(d.citation_coverage_now)} "
        f"(at window start {_fmt_pct(d.citation_coverage_at_since)}, "
        f"moved {_fmt_delta(d.citation_coverage_delta)})"
    )
    return "\n".join(out) + "\n"
