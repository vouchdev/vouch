"""Tier 2 — semantic claim-worthiness scoring (advisory, never at the funnel).

The Tier 1 admission gate (``admission.py``) answers *is this knowledge-shaped?*
— a cheap structural yes/no run at ``proposals._file_proposal``. This module
answers the softer *is it worth remembering?* and does it **advisorily**: it
emits a score in ``[0, 1]`` plus a one-line reason, and something downstream
(``vouch review``, compile) decides what to do with it. It NEVER runs inside the
proposal funnel and NEVER mutates a claim payload, so determinism and byte-offset
receipts are untouched.

The default :class:`HeuristicScorer` is fully local and deterministic — no LLM,
no network. It blends a handful of cheap signals (question / imperative / leading
deixis / has-a-verb / entity presence / length band) with a **novelty** check
against the already-approved KB (an FTS lookup: a claim that near-duplicates an
approved one carries little marginal worth). An opt-in ``LlmScorer`` behind the
``worthiness.scorer: llm`` config is a later addition; the deterministic scorer
is the floor everything else is measured against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import yaml

from . import index_db

if TYPE_CHECKING:
    from .storage import KBStore

# The passive auto-capture actors — same firehoses Tier 1 gates. Worthiness is
# advisory for everyone, but only these are candidates for a configured
# ``defer`` / ``reject`` action; a deliberate author is always annotate-only.
AUTO_CAPTURE_ACTORS: frozenset[str] = frozenset({"vouch-capture", "session-split", "codex"})

DEFAULT_SCORER = "heuristic"
DEFAULT_MIN_SCORE = 0.4
DEFAULT_ACTION = "annotate"

# A near-duplicate is a claim whose content words overlap an approved claim's by
# at least this Jaccard ratio. High so only genuine restatements are flagged.
_DUP_THRESHOLD = 0.8

# Words that carry no topic — stripped before the novelty overlap so two claims
# aren't called duplicates just for sharing "the", "is", "a".
_STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "he",
        "she",
        "they",
        "them",
        "we",
        "you",
        "i",
        "as",
        "by",
        "with",
        "from",
        "into",
        "over",
        "than",
        "then",
        "so",
        "if",
        "not",
        "no",
        "yes",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "must",
    ]
)

# A leading directive verb marks a task issued to the agent ("create pr and
# generate announcement message"), not a durable fact. Kept small and high-signal.
_IMPERATIVE_VERBS: frozenset[str] = frozenset(
    [
        "create",
        "add",
        "run",
        "fix",
        "make",
        "update",
        "implement",
        "generate",
        "write",
        "build",
        "remove",
        "delete",
        "refactor",
        "open",
        "close",
        "merge",
        "push",
        "commit",
        "install",
        "rerun",
        "regenerate",
        "draft",
    ]
)

# A copula/auxiliary is a cheap proxy for "has a finite verb", i.e. states
# something rather than labelling it.
_COPULA_AUX: frozenset[str] = frozenset(
    [
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "must",
    ]
)

# A span that leads with a bare pronoun and no antecedent is not self-contained.
_DEICTIC_LEADS: frozenset[str] = frozenset(
    ["it", "this", "that", "these", "those", "they", "he", "she", "there", "here"]
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
_LEADING_MARKDOWN_RE = re.compile(r"^[>\-*+#\s]+")


@dataclass(frozen=True)
class WorthinessConfig:
    """The ``worthiness:`` block of config.yaml.

    ``scorer`` picks the backend (``heuristic`` | ``llm`` | ``off``). ``min_score``
    is the advisory threshold. ``action`` is what a surface MAY do to a
    sub-threshold auto-capture claim (``annotate`` | ``defer`` | ``reject``);
    ``annotate`` changes nothing on its own.
    """

    scorer: str = DEFAULT_SCORER
    min_score: float = DEFAULT_MIN_SCORE
    action: str = DEFAULT_ACTION
    apply_to: frozenset[str] = AUTO_CAPTURE_ACTORS


def load_config(store: KBStore) -> WorthinessConfig:
    """Read ``worthiness:`` from config.yaml; fall back to defaults on any error."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return WorthinessConfig()
    if not isinstance(loaded, dict):
        return WorthinessConfig()
    raw = loaded.get("worthiness")
    if not isinstance(raw, dict):
        return WorthinessConfig()
    apply_raw = raw.get("apply_to")
    apply_to = (
        frozenset(str(a) for a in apply_raw) if isinstance(apply_raw, list) else AUTO_CAPTURE_ACTORS
    )
    return WorthinessConfig(
        # YAML 1.1 parses a bare ``off`` as boolean False — coerce it back so
        # ``scorer: off`` disables scoring rather than becoming the string "False".
        scorer=_normalize_scorer(raw.get("scorer", DEFAULT_SCORER)),
        min_score=_as_float(raw.get("min_score")) or DEFAULT_MIN_SCORE,
        action=str(raw.get("action", DEFAULT_ACTION)),
        apply_to=apply_to,
    )


def _normalize_scorer(value: object) -> str:
    if value is False:
        return "off"
    return str(value).lower()


@dataclass(frozen=True)
class WorthinessResult:
    """A score in ``[0, 1]``, the dominant reason, and the raw per-signal detail."""

    score: float
    reason: str
    signals: dict[str, float] = field(default_factory=dict)


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _content_tokens(text: str) -> set[str]:
    """Lowercase topic words, stopwords removed — the unit of the novelty overlap."""
    return {w for w in (m.group().lower() for m in _WORD_RE.finditer(text)) if w not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _first_word(text: str) -> str:
    core = _LEADING_MARKDOWN_RE.sub("", text).strip()
    m = _WORD_RE.match(core)
    return m.group().lower() if m else ""


def _has_verb(words: list[str]) -> bool:
    """Crude finite-verb check: a copula/aux, or an inflected non-initial word.

    Deliberately over-inclusive (a plural noun like "claims" reads as a verb),
    because a false *verb* only forgoes a small bonus, whereas a false *no-verb*
    penalises a real claim. The penalty this feeds should fire only on genuine
    noun-phrase labels ("session summary notes") with no inflection at all.
    """
    low = [w.lower() for w in words]
    if set(low) & _COPULA_AUX:
        return True
    for w in low[1:]:
        if w in _STOPWORDS:
            continue
        if w.endswith(("ed", "ing")) or (w.endswith("s") and not w.endswith("ss") and len(w) > 3):
            return True
    return False


def _novelty(text: str, store: KBStore) -> tuple[float, float]:
    """Return ``(novelty, best_overlap)`` against approved claims via FTS.

    ``novelty = 1 - best_overlap``. Fail-open: if the index is unavailable or the
    claim has no content words, treat it as novel (``1.0``) — the novelty signal
    should never punish a claim just because the index could not answer.
    """
    cand = _content_tokens(text)
    if not cand:
        return 1.0, 0.0
    try:
        hits = index_db.search(store.kb_dir, text, limit=5)
    except Exception:
        return 1.0, 0.0
    best = 0.0
    for kind, cid, snippet, _score in hits:
        if kind != "claim":
            continue
        try:
            other = store.get_claim(cid).text
        except Exception:
            other = snippet
        best = max(best, _jaccard(cand, _content_tokens(other)))
    return 1.0 - best, best


class Scorer(Protocol):
    """A worthiness backend. Pluggable like index_db's fts5/embeddings backends."""

    def score(self, text: str, *, store: KBStore) -> WorthinessResult: ...


class HeuristicScorer:
    """Deterministic, local, LLM-free worthiness scoring.

    A blend of cheap signals around a neutral 0.5. Hard shapes (a question, an
    agent-directive imperative) cap the score low; softer signals nudge it; the
    novelty ratio scales the whole thing so a near-duplicate of an approved claim
    lands below a fresh one. The dominant negative signal becomes the reason.
    """

    def score(self, text: str, *, store: KBStore) -> WorthinessResult:
        stripped = text.strip()
        words = [m.group() for m in _WORD_RE.finditer(stripped)]
        n_words = len(words)
        first = _first_word(stripped)
        novelty, overlap = _novelty(stripped, store)

        signals: dict[str, float] = {
            "novelty": round(novelty, 3),
            "words": float(n_words),
        }

        base = 0.5
        reason = "self-contained claim"
        cap = 1.0

        if stripped.endswith("?"):
            cap = 0.15
            reason = "a question, not a claim"
        elif first in _IMPERATIVE_VERBS:
            cap = 0.2
            reason = "an imperative/task, not a durable fact"

        if _has_verb(words):
            base += 0.15
        else:
            base -= 0.1
            if reason == "self-contained claim":
                reason = "no finite verb — reads as a label, not a proposition"

        if first in _DEICTIC_LEADS:
            base -= 0.2
            if reason == "self-contained claim":
                reason = "leads with an unresolved pronoun — not self-contained"

        # An entity — a capitalised word past the first token, or any digit — is a
        # sign the claim is about something specific and durable.
        if any(w[0].isupper() for w in words[1:]) or any(c.isdigit() for c in stripped):
            base += 0.1

        if n_words < 4:
            base -= 0.15
            if reason == "self-contained claim":
                reason = "too short to carry a proposition"
        elif n_words > 60:
            base -= 0.1

        score = max(0.0, min(base, cap)) * novelty
        if overlap >= _DUP_THRESHOLD:
            reason = "near-duplicate of an existing approved claim"
        score = max(0.0, min(1.0, score))
        signals["base"] = round(base, 3)
        return WorthinessResult(score=score, reason=reason, signals=signals)


def get_scorer(cfg: WorthinessConfig) -> Scorer | None:
    """The scorer backend for ``cfg``, or ``None`` when scoring is off."""
    if cfg.scorer == "heuristic":
        return HeuristicScorer()
    # ``llm`` backend lands in a follow-up; unknown values are treated as off so a
    # typo degrades to no-op rather than crashing a review pass.
    return None
