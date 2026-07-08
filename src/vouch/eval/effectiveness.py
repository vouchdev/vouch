"""Per-artifact effectiveness signal from surfaced context vs session outcomes.

Read-only evaluation:
- surfaced artifacts come from the derived `context_surface` cache in state.db
- session outcomes come from append-only audit events

This module never mutates durable KB state.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .. import index_db, metrics
from ..audit import read_events
from ..models import AuditEvent
from ..storage import KBStore

SCHEMA_VERSION = 1
_Z_95 = 1.96
_REJECT_RE = re.compile(r"^proposal\.[a-z]+\.reject$")
_GOOD_EVENTS = frozenset({"claim.confirm"})
_BAD_EVENTS = frozenset({"claim.contradict"})


@dataclass(frozen=True)
class SessionWindow:
    session_id: str
    actor: str
    started_at: datetime
    ended_at: datetime | None


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _in_window(ts: datetime, since: datetime | None, until: datetime | None) -> bool:
    if since is not None and ts < since:
        return False
    return until is None or ts <= until


def _window_contains(win: SessionWindow, ts: datetime) -> bool:
    if ts < win.started_at:
        return False
    if win.ended_at is None:
        return True
    return ts <= win.ended_at


def _wilson_95(successes: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    z2 = _Z_95 ** 2
    denom = 1 + (z2 / total)
    center = (p + (z2 / (2 * total))) / denom
    margin = (
        _Z_95
        * (((p * (1 - p)) / total + (z2 / (4 * total * total))) ** 0.5)
        / denom
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def _build_sessions(events: list[AuditEvent], *, now: datetime) -> dict[str, SessionWindow]:
    sessions: dict[str, SessionWindow] = {}
    ends: dict[str, datetime] = {}
    for ev in events:
        if not ev.object_ids:
            continue
        sid = ev.object_ids[0]
        ts = _as_utc(ev.created_at)
        if ev.event == "session.start":
            sessions[sid] = SessionWindow(
                session_id=sid,
                actor=ev.actor,
                started_at=ts,
                ended_at=None,
            )
        elif ev.event == "session.end":
            ends[sid] = ts
    for sid, end_ts in ends.items():
        win = sessions.get(sid)
        if win is None:
            continue
        sessions[sid] = SessionWindow(
            session_id=win.session_id,
            actor=win.actor,
            started_at=win.started_at,
            ended_at=end_ts,
        )
    # Keep deterministic "still open" end as None; consumers can use now.
    _ = now
    return sessions


def _session_for_event(
    event: AuditEvent,
    *,
    sessions: dict[str, SessionWindow],
    by_actor: dict[str, list[SessionWindow]],
    proposal_to_session: dict[str, str],
) -> str | None:
    if event.event.startswith("session."):
        return None
    # proposal.<kind>.reject points at proposal id in object_ids[0].
    if _REJECT_RE.match(event.event) and event.object_ids:
        sid = proposal_to_session.get(event.object_ids[0])
        if sid is not None:
            return sid
    # If the event explicitly references a session id, trust it.
    for oid in event.object_ids:
        if oid.startswith("sess-") and oid in sessions:
            return oid
    # Otherwise, infer from actor + timestamp overlap.
    ts = _as_utc(event.created_at)
    for win in by_actor.get(event.actor, []):
        if _window_contains(win, ts):
            return win.session_id
    return None


def compute_effectiveness(
    store: KBStore,
    *,
    window: str = "90d",
    min_samples: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return per-artifact outcome association with conservative verdicts."""
    if min_samples < 1:
        raise metrics.MetricsError("--min-samples must be >= 1")
    now_utc = _as_utc(now or datetime.now(UTC))
    since = metrics.parse_since(window, now=now_utc)
    until = now_utc

    events = list(read_events(store.kb_dir))
    sessions = _build_sessions(events, now=now_utc)
    by_actor: dict[str, list[SessionWindow]] = defaultdict(list)
    for s in sessions.values():
        by_actor[s.actor].append(s)
    for rows in by_actor.values():
        rows.sort(key=lambda w: w.started_at)

    proposal_to_session = {
        p.id: p.session_id
        for p in store.list_proposals()
        if p.session_id
    }

    session_outcome: dict[str, str] = {}
    activity: dict[str, set[str]] = defaultdict(set)
    for ev in events:
        ts = _as_utc(ev.created_at)
        if not _in_window(ts, since, until):
            continue
        sid = _session_for_event(
            ev,
            sessions=sessions,
            by_actor=by_actor,
            proposal_to_session=proposal_to_session,
        )
        if sid is None:
            continue
        if ev.event in _GOOD_EVENTS:
            activity[sid].add("good")
        elif ev.event in _BAD_EVENTS or _REJECT_RE.match(ev.event):
            activity[sid].add("bad")

    for sid, marks in activity.items():
        if marks == {"good"}:
            session_outcome[sid] = "good"
        elif marks == {"bad"}:
            session_outcome[sid] = "bad"

    classified_sessions = set(session_outcome)
    total_good = sum(1 for v in session_outcome.values() if v == "good")
    total_bad = sum(1 for v in session_outcome.values() if v == "bad")
    baseline_total = total_good + total_bad
    baseline_rate = (total_good / baseline_total) if baseline_total else None

    surfaced_rows = index_db.read_context_surfaces(
        store.kb_dir,
        since=since,
        until=until,
    )
    by_artifact: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in surfaced_rows:
        sid = row["session_id"]
        if not sid or sid not in classified_sessions:
            continue
        by_artifact[(row["artifact_kind"], row["artifact_id"])].add(sid)

    artifacts: list[dict[str, Any]] = []
    for (kind, artifact_id), surfaced_sessions in sorted(by_artifact.items()):
        surfaced_good = sum(1 for sid in surfaced_sessions if session_outcome[sid] == "good")
        surfaced_bad = sum(1 for sid in surfaced_sessions if session_outcome[sid] == "bad")
        surfaced_total = surfaced_good + surfaced_bad
        if surfaced_total == 0 or baseline_rate is None:
            continue

        exposed_rate = surfaced_good / surfaced_total
        ci_low, ci_high = _wilson_95(surfaced_good, surfaced_total)
        lift = exposed_rate - baseline_rate
        non_exposed_good = total_good - surfaced_good
        non_exposed_bad = total_bad - surfaced_bad

        if surfaced_total < min_samples:
            verdict = "insufficient"
        elif ci_low > baseline_rate:
            verdict = "useful"
        elif ci_high < baseline_rate:
            verdict = "harmful"
        else:
            verdict = "unverified"

        artifacts.append(
            {
                "artifact_kind": kind,
                "artifact_id": artifact_id,
                "samples": surfaced_total,
                "surfaced": {"good": surfaced_good, "bad": surfaced_bad},
                "not_surfaced": {"good": non_exposed_good, "bad": non_exposed_bad},
                "rate": exposed_rate,
                "baseline_rate": baseline_rate,
                "lift": lift,
                "wilson_95": {"low": ci_low, "high": ci_high},
                "verdict": verdict,
                "earned_value": lift * surfaced_total,
            }
        )

    artifacts.sort(
        key=lambda row: (row["earned_value"], row["samples"], row["artifact_id"]),
        reverse=True,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "window": {
            "spec": window,
            "since": since.isoformat() if since else None,
            "until": until.isoformat(),
            "generated_at": now_utc.isoformat(),
        },
        "min_samples": min_samples,
        "sessions": {
            "classified": baseline_total,
            "good": total_good,
            "bad": total_bad,
            "baseline_rate": baseline_rate,
        },
        "artifacts": artifacts,
    }
