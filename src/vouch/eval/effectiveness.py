"""kb.effectiveness — does surfaced knowledge change session outcomes?

vouch can measure retrieval quality (`recall.py`, embeddings evals) but not
whether surfaced knowledge actually helped. This is a read-only,
measurement-only signal: per approved artifact, the association between "it
was in a session's context pack" (`index_db.context_surfacing`, recorded by
`kb.context` when a session_id is given) and a coarse *session outcome*
derived from the audit log — a session whose events lean claim.confirm /
proposal.*.approve is "good"; lean claim.contradict / proposal.*.reject is
"bad"; anything else (including no signal at all) carries no evidence and is
dropped from both sides of the ratio, not folded into either bucket.

Verdicts are conservative on purpose: `useful` / `harmful` render only when
an artifact's 95% Wilson interval on its good-outcome rate clears the
population baseline *and* the sample meets `--min-samples`; otherwise
`unverified` (the interval straddles baseline) or `insufficient` (not enough
sessions yet). An untrustworthy number must never render as a confident
verdict — see vouchdev/vouch#426.

Nothing here writes an artifact, logs an audit event, or files a proposal.
It composes three read-only sources: `store.list_sessions()`,
`audit.read_events()`, and `index_db.read_context_surfacing()`.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..audit import read_events
from ..index_db import read_context_surfacing
from ..storage import KBStore

DEFAULT_WINDOW_SPEC = "90d"
DEFAULT_MIN_SAMPLES = 5

# Schema version baked into the JSON output (see metrics.SCHEMA_VERSION).
SCHEMA_VERSION = 1

# 97.5th percentile of the standard normal — the two-sided 95% Wilson bound.
_Z95 = 1.959963984540054

_APPROVE_RE = re.compile(r"^proposal\.[a-z]+\.approve$")
_REJECT_RE = re.compile(r"^proposal\.[a-z]+\.reject$")
_GOOD_EVENTS = frozenset({"claim.confirm"})
_BAD_EVENTS = frozenset({"claim.contradict"})

_Outcome = str  # "good" | "bad" | "neutral"


class EffectivenessError(ValueError):
    """User-visible bad input (e.g. an unparseable --min-samples)."""


def wilson_interval(k: int, n: int, *, z: float = _Z95) -> tuple[float, float]:
    """95% Wilson score interval for `k` successes out of `n` trials.

    Returns `(0.0, 1.0)` (maximal uncertainty) for `n <= 0` rather than
    dividing by zero — callers gate on `--min-samples` separately, so this
    never has to double as a sample-size check.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = p + z2 / (2 * n)
    adj = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    lo = (center - adj) / denom
    hi = (center + adj) / denom
    return (max(0.0, lo), min(1.0, hi))


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _verdict(
    *, ci_low: float, ci_high: float, n: int, baseline: float | None, min_samples: int,
) -> str:
    if n < min_samples:
        return "insufficient"
    if baseline is None:
        # No population baseline exists (no session in the window carried
        # confirm/contradict/approve/reject signal at all) -- there is
        # nothing to clear, so the number stays unverified rather than
        # defaulting to a confident-sounding verdict.
        return "unverified"
    if ci_low > baseline:
        return "useful"
    if ci_high < baseline:
        return "harmful"
    return "unverified"


@dataclass
class ArtifactEffect:
    """One artifact's row in the effectiveness ranking."""

    kind: str
    id: str
    samples: int
    good: int
    observed_rate: float | None
    baseline_rate: float | None
    lift: float | None
    ci_low: float
    ci_high: float
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "samples": self.samples,
            "good": self.good,
            "observed_rate": self.observed_rate,
            "baseline_rate": self.baseline_rate,
            "lift": self.lift,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "verdict": self.verdict,
        }


@dataclass
class EffectivenessReport:
    """The full `kb.effectiveness` snapshot. `to_dict` is the stable schema."""

    since: datetime | None
    until: datetime | None
    generated_at: datetime
    min_samples: int
    baseline_rate: float | None
    sessions_considered: int
    sessions_with_signal: int
    artifacts: list[ArtifactEffect] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "window": {
                "since": _iso(self.since),
                "until": _iso(self.until),
                "generated_at": _iso(self.generated_at),
            },
            "min_samples": self.min_samples,
            "baseline_rate": self.baseline_rate,
            "sessions_considered": self.sessions_considered,
            "sessions_with_signal": self.sessions_with_signal,
            "artifacts": [a.to_dict() for a in self.artifacts],
        }


def _session_outcomes(store: KBStore, sessions: list[Any]) -> dict[str, _Outcome]:
    """Classify each ended session as good/bad/neutral from the audit log.

    A session's outcome is decided by events attributed to its own agent
    whose timestamp falls inside [started_at, ended_at]: more
    confirm/approve than contradict/reject is "good", the reverse is "bad",
    a tie (including 0-0, the common case for a session with no reviewed
    lifecycle activity) is "neutral" and carries no evidence either way.
    This is deliberately coarse -- concurrent sessions from the same agent
    would blur together -- which is why the signal is reported with a
    confidence interval rather than taken at face value.
    """
    events_by_actor: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for ev in read_events(store.kb_dir):
        ts = _as_utc(ev.created_at)
        if ts is not None:
            events_by_actor[ev.actor].append((ts, ev.event))

    outcomes: dict[str, _Outcome] = {}
    for sess in sessions:
        started = _as_utc(sess.started_at)
        ended = _as_utc(sess.ended_at)
        if started is None or ended is None:
            continue
        good = bad = 0
        for ts, name in events_by_actor.get(sess.agent, ()):
            if ts < started or ts > ended:
                continue
            if name in _GOOD_EVENTS or _APPROVE_RE.match(name):
                good += 1
            elif name in _BAD_EVENTS or _REJECT_RE.match(name):
                bad += 1
        if good > bad:
            outcomes[sess.id] = "good"
        elif bad > good:
            outcomes[sess.id] = "bad"
        else:
            outcomes[sess.id] = "neutral"
    return outcomes


def compute(
    store: KBStore,
    *,
    since: datetime | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    now: datetime | None = None,
) -> EffectivenessReport:
    """Compute the full effectiveness snapshot. Read-only by construction."""
    now = _as_utc(now) or datetime.now(UTC)
    since = _as_utc(since)
    if min_samples < 1:
        raise EffectivenessError("--min-samples must be >= 1")

    sessions = []
    for s in store.list_sessions():
        ended = _as_utc(s.ended_at)
        if ended is not None and (since is None or ended >= since):
            sessions.append(s)

    outcomes = _session_outcomes(store, sessions)
    signal_sessions = {sid: o for sid, o in outcomes.items() if o != "neutral"}
    n_signal = len(signal_sessions)
    good_sessions = sum(1 for o in signal_sessions.values() if o == "good")
    baseline_rate = (good_sessions / n_signal) if n_signal else None

    by_artifact: dict[tuple[str, str], set[str]] = defaultdict(set)
    for session_id, kind, artifact_id, _surfaced_at in read_context_surfacing(store.kb_dir):
        if session_id in signal_sessions:
            by_artifact[(kind, artifact_id)].add(session_id)

    artifacts: list[ArtifactEffect] = []
    for (kind, artifact_id), sess_ids in by_artifact.items():
        n = len(sess_ids)
        k = sum(1 for sid in sess_ids if signal_sessions[sid] == "good")
        observed = (k / n) if n else None
        ci_low, ci_high = wilson_interval(k, n)
        verdict = _verdict(
            ci_low=ci_low, ci_high=ci_high, n=n, baseline=baseline_rate,
            min_samples=min_samples,
        )
        lift = (
            observed - baseline_rate
            if observed is not None and baseline_rate is not None
            else None
        )
        artifacts.append(
            ArtifactEffect(
                kind=kind, id=artifact_id, samples=n, good=k,
                observed_rate=observed, baseline_rate=baseline_rate, lift=lift,
                ci_low=ci_low, ci_high=ci_high, verdict=verdict,
            )
        )

    # Earned-value ranking: the conservative (worst-plausible) lift, i.e. how
    # far the *lower* bound of the interval clears baseline. An artifact with
    # a wide interval around a great point estimate ranks below one with a
    # narrower interval that clears baseline just as convincingly.
    artifacts.sort(
        key=lambda a: (
            -(a.ci_low - (a.baseline_rate or 0.0)),
            a.kind,
            a.id,
        )
    )

    return EffectivenessReport(
        since=since,
        until=now,
        generated_at=now,
        min_samples=min_samples,
        baseline_rate=baseline_rate,
        sessions_considered=len(sessions),
        sessions_with_signal=n_signal,
        artifacts=artifacts,
    )


def render_text(r: EffectivenessReport) -> str:
    lines = [
        f"kb.effectiveness  (window since: {_iso(r.since) or 'all'})",
        f"  sessions considered: {r.sessions_considered}  "
        f"(with outcome signal: {r.sessions_with_signal})",
        "  baseline good-outcome rate: "
        + (f"{r.baseline_rate:.1%}" if r.baseline_rate is not None else "n/a"),
        f"  min samples: {r.min_samples}",
        "",
    ]
    if not r.artifacts:
        lines.append("no surfaced artifacts with outcome signal in this window")
        return "\n".join(lines)
    for a in r.artifacts:
        obs = f"{a.observed_rate:.1%}" if a.observed_rate is not None else "n/a"
        lift = f"{a.lift:+.1%}" if a.lift is not None else "n/a"
        lines.append(
            f"  [{a.verdict:<11}] {a.kind}:{a.id}  n={a.samples}  "
            f"rate={obs}  lift={lift}  ci=[{a.ci_low:.1%}, {a.ci_high:.1%}]"
        )
    return "\n".join(lines)
