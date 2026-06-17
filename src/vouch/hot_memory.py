"""Per-session hot memory — task query and salience snapshots for push context.

Tracks what the active session is working on and the last relevance scores
seen for approved claims. ``volunteer_context`` diffs snapshots to decide when
a claim newly crosses the confidence threshold.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class SalienceSnapshot:
    """Relevance scores for a single evaluation pass."""

    scores: dict[str, float] = field(default_factory=dict)


@dataclass
class HotMemory:
    """In-memory state for one active session watch."""

    session_id: str
    query: str
    agent: str
    project: str | None = None
    last_snapshot: SalienceSnapshot = field(default_factory=SalienceSnapshot)
    last_push_at: float | None = None
    push_count: int = 0
    volunteered: set[str] = field(default_factory=set)
    active: bool = True


_registry: dict[str, HotMemory] = {}
_lock = threading.Lock()


def register(
    *,
    session_id: str,
    query: str,
    agent: str,
    project: str | None = None,
) -> HotMemory:
    """Create or replace hot memory for *session_id*."""
    mem = HotMemory(
        session_id=session_id,
        query=query,
        agent=agent,
        project=project,
    )
    with _lock:
        _registry[session_id] = mem
    return mem


def get(session_id: str) -> HotMemory | None:
    with _lock:
        return _registry.get(session_id)


def unregister(session_id: str) -> None:
    with _lock:
        mem = _registry.pop(session_id, None)
        if mem is not None:
            mem.active = False


def update_snapshot(session_id: str, scores: dict[str, float]) -> SalienceSnapshot | None:
    """Store *scores* and return the previous snapshot (for delta detection)."""
    with _lock:
        mem = _registry.get(session_id)
        if mem is None:
            return None
        prev = mem.last_snapshot
        mem.last_snapshot = SalienceSnapshot(scores=dict(scores))
        return prev


def mark_volunteered(session_id: str, claim_id: str, *, pushed_at: float) -> None:
    with _lock:
        mem = _registry.get(session_id)
        if mem is None:
            return
        mem.volunteered.add(claim_id)
        mem.last_push_at = pushed_at
        mem.push_count += 1
