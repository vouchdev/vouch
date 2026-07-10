"""Correction-capture — turn a user pushback into a *pending* draft claim.

The single highest-signal event in a session is the user correcting the agent
("no, we deploy from `main` not `release`"). Passive tool-capture
(`capture.observe`) never sees it, and it evaporates unless someone proposes a
claim by hand. This module detects that turn cheaply (no LLM) and, when it
fires, files ONE pending claim proposal quoting the correction.

The whole design constraint: it **proposes, never writes**. There is no code
path from here to `proposals.approve` — the correction lands in the pending
queue as a normal draft tagged `auto:correction`, and a human still drains it.
Because it routes exclusively through `proposals.propose_claim`, it cannot
create approved knowledge and cannot bypass the review gate.

Guards against swamping the reviewer:
  - a per-session cap on auto-proposals (`capture.correction.per_session_cap`);
  - dedup against near-duplicate approved/pending claims via the #147
    similarity path, plus a cheap embeddings-free check for exact repeats.

Config (read defensively from ``.vouch/config.yaml``):
  - ``capture.correction.enabled`` (default True)
  - ``capture.correction.per_session_cap`` (default 3)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from .models import ProposalKind
from .proposals import propose_claim
from .storage import KBStore

CORRECTION_ACTOR = "auto:correction"
CORRECTION_TAG = "auto:correction"
DEFAULT_ENABLED = True
DEFAULT_PER_SESSION_CAP = 3
_MAX_CLAIM_CHARS = 500

# Leading pushback markers — a negation/"actually" at the very start of the
# turn is the strongest cheap signal that the user is correcting the agent.
# Punctuation is required on the bare "no" forms so an innocent "no idea why…"
# doesn't trip the heuristic.
_OPENERS = (
    "no,", "no.", "no!", "nope", "not quite",
    "actually,", "actually ", "wrong,", "incorrect,",
    "that's not", "that is not", "that's wrong", "that is wrong",
    "that's incorrect", "that is incorrect",
    "you're wrong", "you are wrong", "correction:",
)
# Strong corrective phrases that count anywhere in a short turn.
_PHRASES = (
    "we don't ", "we do not ", "it's not ", "it is not ",
    "that's not right", "not from ",
)
# Prefixes stripped off the front of the captured correction so the claim
# quotes the corrective content, not the pushback marker.
_STRIP_PREFIXES = (
    "no,", "no.", "no!", "no ", "nope,", "nope.", "nope ",
    "actually,", "actually ", "wrong,", "incorrect,", "correction:",
    "that's wrong,", "that is wrong,", "that's incorrect,",
)


@dataclass(frozen=True)
class CorrectionConfig:
    """Resolved ``capture.correction`` config."""

    enabled: bool = DEFAULT_ENABLED
    per_session_cap: int = DEFAULT_PER_SESSION_CAP


def load_config(store: KBStore) -> CorrectionConfig:
    """Read ``capture.correction`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return CorrectionConfig()
    if not isinstance(loaded, dict):
        return CorrectionConfig()
    capture = loaded.get("capture")
    raw = capture.get("correction") if isinstance(capture, dict) else None
    if not isinstance(raw, dict):
        return CorrectionConfig()
    cap = raw.get("per_session_cap", DEFAULT_PER_SESSION_CAP)
    cap = cap if isinstance(cap, int) and cap >= 0 else DEFAULT_PER_SESSION_CAP
    return CorrectionConfig(
        enabled=bool(raw.get("enabled", DEFAULT_ENABLED)),
        per_session_cap=cap,
    )


def _strip_prefix(text: str) -> str:
    low = text.lower()
    for prefix in _STRIP_PREFIXES:
        if low.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def detect_correction(prompt: str) -> str | None:
    """Return the corrective content if ``prompt`` reads like a user correction.

    Cheap heuristic (no LLM): a pushback marker leading the turn, or a strong
    corrective phrase anywhere in it. Returns the corrective text with the
    leading marker stripped, or ``None`` when no correction is detected. Tuned
    to accept false positives — every hit is only a *proposal* a human reviews,
    and the per-session cap bounds an over-eager trigger.
    """
    text = (prompt or "").strip()
    if not text:
        return None
    low = text.lower()
    matched = low.startswith(_OPENERS) or any(p in low for p in _PHRASES)
    if not matched:
        return None
    corrective = _strip_prefix(text).strip() or text
    return corrective[:_MAX_CLAIM_CHARS]


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _session_correction_count(store: KBStore, session_id: str | None) -> int:
    """How many auto:correction proposals this session has already filed."""
    return sum(
        1 for p in store.list_proposals()
        if p.proposed_by == CORRECTION_ACTOR and p.session_id == session_id
    )


def _is_duplicate(store: KBStore, corrective: str) -> bool:
    """True if ``corrective`` duplicates an approved/pending claim.

    Uses the #147 similarity path (semantic, when the embeddings extra is
    installed) and a cheap embeddings-free check for an exact repeat already
    sitting in the pending queue.
    """
    try:
        from .embeddings.similarity import find_similar_on_propose
        if find_similar_on_propose(store, corrective):
            return True
    except ImportError:
        pass
    target = _norm(corrective)
    for p in store.list_proposals():
        if p.kind != ProposalKind.CLAIM:
            continue
        if _norm(str(p.payload.get("text", ""))) == target:
            return True
    return False


def capture_correction(
    store: KBStore,
    *,
    prompt: str,
    session_id: str | None = None,
    config: CorrectionConfig | None = None,
) -> dict[str, Any]:
    """Detect a correction in ``prompt`` and enqueue one PENDING claim. No approve().

    Returns a result dict describing what happened: ``captured`` plus either a
    ``proposal_id`` / ``source_id`` or a ``skipped`` reason
    (``disabled`` / ``no-correction`` / ``session-cap`` / ``duplicate``).
    """
    cfg = config or load_config(store)
    if not cfg.enabled:
        return {"captured": False, "skipped": "disabled"}

    corrective = detect_correction(prompt)
    if corrective is None:
        return {"captured": False, "skipped": "no-correction"}

    if _session_correction_count(store, session_id) >= cfg.per_session_cap:
        return {"captured": False, "skipped": "session-cap", "correction": corrective}

    if _is_duplicate(store, corrective):
        return {"captured": False, "skipped": "duplicate", "correction": corrective}

    # The correction message itself is the evidence — register it as a
    # content-addressed message source so the claim cites something concrete
    # (and identical corrections collapse to the same source id).
    source = store.put_source(
        corrective.encode("utf-8"),
        title=f"user correction ({session_id or 'session'})",
        source_type="message",
        metadata={"origin": CORRECTION_ACTOR, "session_id": session_id},
    )
    result = propose_claim(
        store,
        text=corrective,
        evidence=[source.id],
        proposed_by=CORRECTION_ACTOR,
        tags=[CORRECTION_TAG],
        rationale="captured from user correction",
        session_id=session_id,
    )
    return {
        "captured": True,
        "proposal_id": result.proposal.id,
        "source_id": source.id,
        "correction": corrective,
    }
