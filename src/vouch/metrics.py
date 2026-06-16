"""First-class observability metrics for a vouch KB (vouchdev/vouch#192).

``vouch status`` answers "is the KB alive?" — artifact counts. It does *not*
answer "is the **review gate** working?": how often proposals get approved,
how stale the corpus is, how long claims sit pending, who is doing the
proposing and approving. Operators want those signals so they can graph them
over time and alert on regressions.

Everything here derives **purely** from two read-only sources that already
exist on disk — no new state, no schema migration:

* ``.vouch/audit.log.jsonl`` — the append-only event stream. Every proposal
  create / approve / reject and every claim lifecycle event lands here with a
  timestamp, actor and object ids.
* the artifact files themselves (``claims/*.yaml``, ``sources/*``,
  ``evidence/*``) — read through the normal :class:`~vouch.storage.KBStore`
  API so the same validation that protects the rest of vouch protects us too.

The output schema is **stable and documented** in ``docs/metrics.md``; the
``--json`` form is the contract other tools build on (a Prometheus textfile
sidecar, a dashboard, a CI gate). Treat field renames as breaking.

Design notes
------------
* **Window first.** Every audit-derived metric is computed over a time window
  (``--since``); the default window is "all of history". The window is applied
  to the event's ``created_at``. Artifact-derived metrics (citation coverage,
  stale ratio) reflect *current* on-disk state and are intentionally
  window-independent — a stale claim is stale now regardless of when you look.
* **Approve/reject come from the audit log, not proposal files.** ``decided/``
  proposals would undercount if a KB pruned them; the audit log is append-only
  and authoritative, and it carries the timestamps the lag percentiles need.
* **Percentiles use nearest-rank**, the same definition Prometheus
  ``histogram_quantile`` approximates, so p50/p90 line up with what an operator
  expects from a dashboard.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .audit import read_events
from .models import AuditEvent, ClaimStatus
from .storage import KBStore

# How long a claim may go un-confirmed before it counts as "stale". Mirrors
# the default in ``health.lint`` so ``vouch metrics`` and ``vouch lint`` agree
# on what stale means.
DEFAULT_STALE_DAYS = 180

# Default number of actors to surface in the leaderboard.
DEFAULT_TOP_ACTORS = 5

# The dotted-event verbs we care about. Proposal verbs embed the kind
# (``proposal.claim.approve``), so we match on a regex rather than a literal
# set — that way a new ProposalKind needs no change here.
_CREATE_RE = re.compile(r"^proposal\.(?P<kind>[a-z]+)\.create$")
_APPROVE_RE = re.compile(r"^proposal\.(?P<kind>[a-z]+)\.approve$")
_REJECT_RE = re.compile(r"^proposal\.(?P<kind>[a-z]+)\.reject$")

# Claim lifecycle verbs that count as "this actor touched the corpus" for the
# actor leaderboard's confirm column.
_CONFIRM_EVENTS = frozenset({"claim.confirm"})

# Schema version baked into the JSON output. Bump on any breaking field
# change so downstream consumers can refuse a shape they don't understand.
SCHEMA_VERSION = 1


# --- time windows ---------------------------------------------------------


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


class MetricsError(ValueError):
    """User-visible bad input (e.g. an unparseable ``--since``)."""


def parse_since(spec: str | None, *, now: datetime | None = None) -> datetime | None:
    """Resolve a ``--since`` spec to an absolute cutoff (or ``None`` = all time).

    Accepts:

    * ``None`` / empty / ``"all"`` -> ``None`` (no lower bound).
    * a relative duration: ``30d``, ``12h``, ``2w``, ``90m``, ``45s`` — counted
      back from ``now`` (defaults to the current UTC time).
    * an ISO-8601 timestamp or date: ``2026-01-01`` or
      ``2026-01-01T00:00:00+00:00`` (naive values are read as UTC).

    Raises :class:`MetricsError` on anything else, with the offending text in
    the message so the CLI can show a clean error rather than a traceback.
    """
    if spec is None:
        return None
    text = spec.strip()
    if not text or text.lower() == "all":
        return None

    now = now or datetime.now(UTC)

    m = _DURATION_RE.match(text)
    if m:
        qty = int(m.group(1))
        unit = m.group(2).lower()
        return now - qty * _DURATION_UNITS[unit]

    # Fall back to ISO-8601. ``date`` (no time) is allowed; pad it to midnight.
    iso = text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as e:
        raise MetricsError(
            f"can't parse --since {spec!r}: expected a duration like '30d' / "
            f"'12h' / '2w' or an ISO date like '2026-01-01' ({e})"
        ) from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --- result containers ----------------------------------------------------


@dataclass(frozen=True)
class LagStats:
    """Latency, in seconds, from a proposal's ``create`` to its ``approve``.

    ``count`` is how many approved proposals we could pair with a create event
    inside the window; the percentiles are ``None`` when ``count == 0`` so the
    JSON consumer can tell "no data" from "zero latency".
    """

    count: int = 0
    p50: float | None = None
    p90: float | None = None
    p99: float | None = None
    mean: float | None = None
    max: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "p50": self.p50,
            "p90": self.p90,
            "p99": self.p99,
            "mean": self.mean,
            "max": self.max,
        }


@dataclass(frozen=True)
class ActorStat:
    """One row of the actor leaderboard."""

    actor: str
    proposed: int = 0
    approved: int = 0
    rejected: int = 0
    confirmed: int = 0

    @property
    def total(self) -> int:
        return self.proposed + self.approved + self.rejected + self.confirmed

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "proposed": self.proposed,
            "approved": self.approved,
            "rejected": self.rejected,
            "confirmed": self.confirmed,
            "total": self.total,
        }


@dataclass
class Metrics:
    """The full observability snapshot. ``to_dict`` is the stable schema."""

    # Window the audit-derived numbers were computed over.
    since: datetime | None = None
    until: datetime | None = None
    generated_at: datetime | None = None

    # Review-gate health.
    approvals: int = 0
    rejections: int = 0
    approval_rate: float | None = None  # None when no decisions in window
    approval_rate_by_kind: dict[str, float] = field(default_factory=dict)
    decisions_by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    proposals_created: int = 0
    pending_now: int = 0

    # Corpus health (current state, window-independent).
    claims_total: int = 0
    claims_active: int = 0
    claims_cited: int = 0
    citation_coverage: float | None = None
    citation_broken: int = 0
    stale_claims: int = 0
    stale_ratio: float | None = None
    stale_after_days: int = DEFAULT_STALE_DAYS
    claims_by_status: dict[str, int] = field(default_factory=dict)

    # Latency + people.
    proposal_lag: LagStats = field(default_factory=LagStats)
    actors: list[ActorStat] = field(default_factory=list)

    # Raw event volume (handy for "is anything happening at all?").
    audit_events_total: int = 0
    audit_events_in_window: int = 0

    def to_dict(self) -> dict[str, Any]:
        """The documented, machine-readable schema (``docs/metrics.md``)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "window": {
                "since": _iso(self.since),
                "until": _iso(self.until),
                "generated_at": _iso(self.generated_at),
            },
            "review_gate": {
                "proposals_created": self.proposals_created,
                "approvals": self.approvals,
                "rejections": self.rejections,
                "approval_rate": self.approval_rate,
                "approval_rate_by_kind": self.approval_rate_by_kind,
                "decisions_by_kind": self.decisions_by_kind,
                "pending_now": self.pending_now,
            },
            "corpus": {
                "claims_total": self.claims_total,
                "claims_active": self.claims_active,
                "claims_cited": self.claims_cited,
                "citation_coverage": self.citation_coverage,
                "citation_broken": self.citation_broken,
                "stale_claims": self.stale_claims,
                "stale_ratio": self.stale_ratio,
                "stale_after_days": self.stale_after_days,
                "claims_by_status": self.claims_by_status,
            },
            "proposal_lag_seconds": self.proposal_lag.to_dict(),
            "actors": [a.to_dict() for a in self.actors],
            "audit": {
                "events_total": self.audit_events_total,
                "events_in_window": self.audit_events_in_window,
            },
        }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# --- percentile helper ----------------------------------------------------


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list.

    ``q`` is a fraction in ``[0, 1]``. Returns ``None`` for an empty list.
    Nearest-rank (rather than linear interpolation) matches the bucket-based
    estimate Prometheus' ``histogram_quantile`` produces, so a dashboard built
    on the scraped values and this CLI agree.
    """
    if not sorted_values:
        return None
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    # rank in 1..n
    rank = math.ceil(q * len(sorted_values))
    rank = max(1, min(rank, len(sorted_values)))
    return sorted_values[rank - 1]


# --- the computation ------------------------------------------------------


def _in_window(ev: AuditEvent, since: datetime | None, until: datetime | None) -> bool:
    ts = _as_utc(ev.created_at)
    if ts is None:
        return False
    if since is not None and ts < since:
        return False
    return until is None or ts <= until


def compute(
    store: KBStore,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    stale_after_days: int = DEFAULT_STALE_DAYS,
    top_actors: int = DEFAULT_TOP_ACTORS,
    now: datetime | None = None,
) -> Metrics:
    """Compute the full :class:`Metrics` snapshot for ``store``.

    The audit log is streamed once; artifacts are loaded once. Everything is
    O(events + claims) — cheap enough to run on every CI step.
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

    m = Metrics(
        since=since,
        until=until,
        generated_at=now,
        stale_after_days=stale_after_days,
    )

    # --- pass over the audit log ---
    approvals = 0
    rejections = 0
    created = 0
    by_kind_decisions: dict[str, dict[str, int]] = defaultdict(
        lambda: {"approve": 0, "reject": 0}
    )
    proposed_counter: Counter[str] = Counter()
    approved_counter: Counter[str] = Counter()
    rejected_counter: Counter[str] = Counter()
    confirmed_counter: Counter[str] = Counter()
    # proposal id -> create timestamp (for lag pairing). Build from *all*
    # create events (even outside the window) so an approval inside the window
    # whose create is older still pairs — otherwise the lag would be
    # systematically undercounted at the window's left edge.
    create_ts: dict[str, datetime] = {}
    lag_samples: list[float] = []
    events_total = 0
    events_in_window = 0

    for ev in read_events(store.kb_dir):
        events_total += 1
        ts = _as_utc(ev.created_at)

        # Record every create timestamp regardless of window (see above).
        cm = _CREATE_RE.match(ev.event)
        if cm and ev.object_ids and ts is not None:
            create_ts[ev.object_ids[0]] = ts

        in_window = _in_window(ev, since, until)
        if in_window:
            events_in_window += 1

        if not in_window:
            continue

        if cm:
            created += 1
            proposed_counter[ev.actor] += 1
            continue

        am = _APPROVE_RE.match(ev.event)
        if am:
            approvals += 1
            approved_counter[ev.actor] += 1
            by_kind_decisions[am.group("kind")]["approve"] += 1
            # object_ids[0] is the proposal id (proposals.approve logs
            # [proposal.id, result.id]); pair it with its create event.
            if ev.object_ids:
                created_at = create_ts.get(ev.object_ids[0])
                if created_at is not None and ts is not None and ts >= created_at:
                    lag_samples.append((ts - created_at).total_seconds())
            continue

        rm = _REJECT_RE.match(ev.event)
        if rm:
            rejections += 1
            rejected_counter[ev.actor] += 1
            by_kind_decisions[rm.group("kind")]["reject"] += 1
            continue

        if ev.event in _CONFIRM_EVENTS:
            confirmed_counter[ev.actor] += 1

    m.approvals = approvals
    m.rejections = rejections
    m.proposals_created = created
    decided = approvals + rejections
    m.approval_rate = (approvals / decided) if decided else None
    m.decisions_by_kind = {k: dict(v) for k, v in sorted(by_kind_decisions.items())}
    m.approval_rate_by_kind = {
        k: (v["approve"] / (v["approve"] + v["reject"]))
        for k, v in m.decisions_by_kind.items()
        if (v["approve"] + v["reject"]) > 0
    }
    m.audit_events_total = events_total
    m.audit_events_in_window = events_in_window

    # --- proposal lag percentiles ---
    lag_samples.sort()
    if lag_samples:
        m.proposal_lag = LagStats(
            count=len(lag_samples),
            p50=percentile(lag_samples, 0.50),
            p90=percentile(lag_samples, 0.90),
            p99=percentile(lag_samples, 0.99),
            mean=sum(lag_samples) / len(lag_samples),
            max=lag_samples[-1],
        )
    else:
        m.proposal_lag = LagStats(count=0)

    # --- actor leaderboard ---
    actor_names = (
        set(proposed_counter)
        | set(approved_counter)
        | set(rejected_counter)
        | set(confirmed_counter)
    )
    rows = [
        ActorStat(
            actor=name,
            proposed=proposed_counter[name],
            approved=approved_counter[name],
            rejected=rejected_counter[name],
            confirmed=confirmed_counter[name],
        )
        for name in actor_names
    ]
    # Most active first; ties broken alphabetically for deterministic output.
    rows.sort(key=lambda r: (-r.total, r.actor))
    m.actors = rows[:top_actors] if top_actors > 0 else rows

    # --- corpus health (current on-disk state) ---
    _fill_corpus_metrics(store, m, now=now, stale_after_days=stale_after_days)

    return m


def _fill_corpus_metrics(
    store: KBStore,
    m: Metrics,
    *,
    now: datetime,
    stale_after_days: int,
) -> None:
    """Citation coverage, stale ratio and the status histogram from artifacts.

    Mirrors ``health.lint``'s notions of "cited" (every evidence ref resolves
    to a Source or Evidence id) and "stale" (no confirm/update in N days) so
    the two surfaces never disagree.
    """
    claims = store.list_claims()
    source_ids = {s.id for s in store.list_sources()}
    evidence_ids = {e.id for e in store.list_evidence()}
    resolvable = source_ids | evidence_ids

    # Statuses that count as "live" corpus (everything not retired). Stale
    # ratio is measured against this denominator, matching lint, which only
    # warns on claims a reader would still trust.
    retired = {ClaimStatus.SUPERSEDED, ClaimStatus.ARCHIVED, ClaimStatus.REDACTED}

    status_hist: Counter[str] = Counter()
    cited = 0
    broken = 0
    stale = 0
    active = 0
    threshold = timedelta(days=stale_after_days)

    for c in claims:
        status_hist[c.status.value] += 1
        is_active = c.status not in retired
        if is_active:
            active += 1

        # Citation coverage: at least one ref AND every ref resolves.
        refs = list(c.evidence)
        if refs and all(r in resolvable for r in refs):
            cited += 1
        if any(r not in resolvable for r in refs):
            broken += 1

        # Stale: live claims whose freshness anchor is older than the
        # threshold. Retired claims are intentionally exempt — they're not
        # expected to be refreshed.
        if is_active:
            anchor = _as_utc(c.last_confirmed_at or c.updated_at or c.created_at)
            if anchor is not None and (now - anchor) > threshold:
                stale += 1

    total = len(claims)
    m.claims_total = total
    m.claims_active = active
    m.claims_cited = cited
    m.citation_coverage = (cited / total) if total else None
    m.citation_broken = broken
    m.stale_claims = stale
    m.stale_ratio = (stale / active) if active else None
    m.claims_by_status = dict(sorted(status_hist.items()))

    # pending proposals "right now" — a review-gate backlog signal.
    from .models import ProposalStatus

    m.pending_now = len(store.list_proposals(ProposalStatus.PENDING))


# --- Prometheus textfile rendering ----------------------------------------


def _prom_line(
    name: str,
    value: float | int | None,
    labels: dict[str, str] | None = None,
) -> str | None:
    """One Prometheus exposition line, or ``None`` to skip a null gauge.

    Prometheus has no notion of "missing", so a ``None`` metric (e.g.
    ``approval_rate`` with zero decisions) is omitted rather than rendered as
    ``0`` — emitting ``0`` would lie about the denominator.
    """
    if value is None:
        return None
    if labels:
        label_str = ",".join(f'{k}="{_prom_escape(v)}"' for k, v in sorted(labels.items()))
        head = f"{name}{{{label_str}}}"
    else:
        head = name
    if isinstance(value, bool):  # guard: bool is an int subclass
        value = int(value)
    return f"{head} {value}"


def _prom_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(m: Metrics, *, prefix: str = "vouch") -> str:
    """Render the snapshot as Prometheus textfile-collector format.

    Compatible with ``node_exporter --collector.textfile`` and the Pushgateway:
    write the output to ``<textfile_dir>/vouch.prom`` from a cron sidecar and
    the numbers show up as gauges. ``# HELP``/``# TYPE`` headers are emitted so
    the exposition is self-describing.
    """
    lines: list[str] = []
    emitted_headers: set[str] = set()

    def gauge(name: str, help_text: str, value, labels=None) -> None:
        full = f"{prefix}_{name}"
        rendered = _prom_line(full, value, labels)
        if rendered is None:
            return
        # Emit HELP/TYPE once per metric name.
        header_key = full
        if header_key not in emitted_headers:
            lines.append(f"# HELP {full} {help_text}")
            lines.append(f"# TYPE {full} gauge")
            emitted_headers.add(header_key)
        lines.append(rendered)

    gauge("proposals_created", "Proposals created in window.", m.proposals_created)
    gauge("approvals_total", "Approvals in window.", m.approvals)
    gauge("rejections_total", "Rejections in window.", m.rejections)
    gauge("approval_rate", "approve / (approve + reject) over window.", m.approval_rate)
    gauge("pending_proposals", "Pending proposals right now.", m.pending_now)
    gauge("claims_total", "Total claims on disk.", m.claims_total)
    gauge("claims_active", "Non-retired claims.", m.claims_active)
    gauge("citation_coverage", "Fraction of claims fully cited.", m.citation_coverage)
    gauge("citation_broken", "Claims with an unresolved citation.", m.citation_broken)
    gauge("stale_claims", "Active claims past the stale threshold.", m.stale_claims)
    gauge("stale_ratio", "stale_claims / claims_active.", m.stale_ratio)
    gauge("proposal_lag_seconds_p50", "p50 create->approve latency.", m.proposal_lag.p50)
    gauge("proposal_lag_seconds_p90", "p90 create->approve latency.", m.proposal_lag.p90)
    gauge("proposal_lag_seconds_p99", "p99 create->approve latency.", m.proposal_lag.p99)
    gauge("audit_events_total", "All audit events ever.", m.audit_events_total)
    gauge("audit_events_in_window", "Audit events in window.", m.audit_events_in_window)

    # Per-kind approval rate as a labelled gauge.
    for kind, rate in sorted(m.approval_rate_by_kind.items()):
        gauge("approval_rate_by_kind", "Per-kind approval rate.", rate, {"kind": kind})

    # Per-status claim histogram.
    for status, count in sorted(m.claims_by_status.items()):
        gauge("claims_by_status", "Claim count per status.", count, {"status": status})

    # Per-actor proposal/approval counts.
    for a in m.actors:
        gauge("actor_proposed", "Proposals by actor in window.", a.proposed, {"actor": a.actor})
        gauge("actor_approved", "Approvals by actor in window.", a.approved, {"actor": a.actor})

    return "\n".join(lines) + ("\n" if lines else "")
