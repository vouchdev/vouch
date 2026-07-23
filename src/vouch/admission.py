"""Deterministic admission gate: reject knowledge-shaped garbage before it is
filed, keyed on provenance.

vouch has always had a gate for *who approves* a write (``proposals.approve``);
it has never had one for *whether the content is knowledge-shaped*. Every
ingestion path funnels through ``proposals._file_proposal``, so this predicate —
applied there — is the single, drift-free place to raise the floor.

Rules, all cheap, deterministic, and receipt-safe. They only accept or reject a
verbatim payload; they never rewrite it, so byte-offset receipts stay intact:

* **L1 structural floor (claims).** A claim whose text is a fragment — a
  markdown heading (``##``+), a lead-in ending in a colon, or a truncated span
  with an unbalanced code span / bracket — is not a claim. Tuned for high
  precision: episodic-but-grammatical prose is *not* caught here (that judgment
  is advisory, not a regex's to make), and rules that misfire on real claims
  (stranded prepositions, ``**kwargs``, ``27"``) were deliberately dropped.
* **Confidence floor (claims).** Optionally, a claim whose ``confidence`` is
  below ``admission.min_confidence`` is rejected. Off by default (floor ``0.0``)
  so it changes nothing until an operator opts in or a scoring layer starts
  assigning varied per-claim confidence.
* **L0/L2 metadata rule (pages).** An auto-captured page of ``type: session`` /
  ``log`` that cites nothing is a session diary, not durable knowledge — the
  same exclusion ``compile._FORBIDDEN_TYPES`` already applies downstream, moved
  upstream to admission.

The gate only *blocks* for the passive auto-capture actors (the firehoses). For
a deliberate author — an agent calling ``kb_propose_claim``, a human at the CLI,
a hub import — the verdict is advisory: the write still goes through. Someone
chose to file it; the review gate, not a heuristic, decides its fate.

Everything is configurable under the ``admission:`` block of ``config.yaml``
(see ``AdmissionConfig``); auto-rejections are recorded (``decided_by:
vouch-admission``) and reviewable with ``vouch rejected``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .storage import KBStore

# Passive session-capture actors whose proposals are auto-rejected on a failed
# admission check. Deliberate / human / downstream actors are advisory-only.
AUTO_CAPTURE_ACTORS: frozenset[str] = frozenset(
    {"vouch-capture", "session-split", "codex"}
)

# ``session`` / ``log`` pages are raw material, not topics — a mirror of
# ``compile._FORBIDDEN_TYPES``, enforced here at admission instead of at compile.
_RAW_PAGE_TYPES: frozenset[str] = frozenset({"session", "log"})

_MIN_LETTER_RATIO = 0.5
# Two or more leading hashes = a markdown heading. A single '#' is left alone:
# it is ambiguous with the "# of X" ("number of") idiom, and rejecting real
# claims is worse than letting a rare single-'#' heading reach human review.
_HEADING_RE = re.compile(r"^#{2,6}\s")
_LEADING_MARKDOWN_RE = re.compile(r"^[>\-*+\s]+")

DEFAULT_ENABLED = True
DEFAULT_MIN_CONFIDENCE = 0.0
DEFAULT_REJECT_UNCITED_SESSION_PAGES = True


@dataclass(frozen=True)
class AdmissionConfig:
    """The ``admission:`` block of ``config.yaml``.

    ``enabled`` is the master switch. ``min_confidence`` is the claim confidence
    floor (``0.0`` = off). ``reject_uncited_session_pages`` toggles the page
    metadata rule. All only ever affect auto-capture actors.
    """

    enabled: bool = DEFAULT_ENABLED
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    reject_uncited_session_pages: bool = DEFAULT_REJECT_UNCITED_SESSION_PAGES


def load_config(store: KBStore) -> AdmissionConfig:
    """Read ``admission:`` from config.yaml; fall back to defaults on any error."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return AdmissionConfig()
    if not isinstance(loaded, dict):
        return AdmissionConfig()
    raw = loaded.get("admission")
    if not isinstance(raw, dict):
        return AdmissionConfig()
    return AdmissionConfig(
        enabled=bool(raw.get("enabled", DEFAULT_ENABLED)),
        min_confidence=_as_float(raw.get("min_confidence")) or DEFAULT_MIN_CONFIDENCE,
        reject_uncited_session_pages=bool(
            raw.get("reject_uncited_session_pages", DEFAULT_REJECT_UNCITED_SESSION_PAGES)
        ),
    )


@dataclass(frozen=True)
class AdmissionVerdict:
    admit: bool
    reason: str | None = None


_ADMIT = AdmissionVerdict(admit=True)


def _reject(reason: str) -> AdmissionVerdict:
    return AdmissionVerdict(admit=False, reason=reason)


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _delimiters_balanced(s: str) -> bool:
    """True unless an inline-code span or a bracket is left unpaired.

    An odd count of ```` ` ```` — or a bracket that closes without an opener /
    opens without a close — is the signature of a span the capture splitter cut
    mid-syntax. Deliberately NOT checked: ``**`` (rejects ``**kwargs``) and ``"``
    (rejects ``a 27" monitor``); both are common in real claims and neither
    caught any observed fragment.
    """
    if s.count("`") % 2:
        return False
    pairs = {")": "(", "]": "[", "}": "{"}
    openers = set(pairs.values())
    stack: list[str] = []
    for ch in s:
        if ch in openers:
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack[-1] != pairs[ch]:
                return False  # stray close — truncated head
            stack.pop()
    return not stack  # leftover open — truncated tail


def assess_claim(
    text: str, *, confidence: float | None = None, min_confidence: float = 0.0
) -> AdmissionVerdict:
    """Structural floor (always) + optional confidence floor for a claim span."""
    stripped = text.strip()
    if _HEADING_RE.match(stripped):
        return _reject("markdown heading, not a claim")
    core = _LEADING_MARKDOWN_RE.sub("", stripped).strip()
    if not core:
        return _reject("empty after stripping markdown markers")
    letters = sum(c.isalpha() for c in core)
    if letters < len(core) * _MIN_LETTER_RATIO:
        return _reject("mostly punctuation/markup, not prose")
    if core.rstrip().endswith(":"):
        return _reject("lead-in ending in a colon, not a standalone claim")
    if not _delimiters_balanced(core):
        return _reject("unbalanced delimiters — a truncated fragment")
    # NOTE: no "ends on a dangling function word" rule — English claims routinely
    # end in a stranded preposition ("...the config lives in.") and rejecting
    # them silently loses real knowledge. Precision over recall here.
    if confidence is not None and confidence < min_confidence:
        return _reject(
            f"confidence {confidence:.2f} below admission floor {min_confidence:.2f}"
        )
    return _ADMIT


def assess_page(payload: dict) -> AdmissionVerdict:
    """Metadata rule: an uncited ``session``/``log`` page is a diary, not a topic."""
    page_type = payload.get("type")
    if page_type in _RAW_PAGE_TYPES and not (
        payload.get("claims") or payload.get("sources")
    ):
        return _reject(
            f"uncited {page_type!r} page — a session diary, not durable knowledge"
        )
    return _ADMIT


def assess(kind: str, payload: dict, cfg: AdmissionConfig | None = None) -> AdmissionVerdict:
    """Dispatch on proposal kind under ``cfg``. Entities/relations/deletes admit."""
    cfg = cfg or AdmissionConfig()
    if not cfg.enabled:
        return _ADMIT
    if kind == "claim":
        return assess_claim(
            str(payload.get("text", "")),
            confidence=_as_float(payload.get("confidence")),
            min_confidence=cfg.min_confidence,
        )
    if kind == "page":
        if not cfg.reject_uncited_session_pages:
            return _ADMIT
        return assess_page(payload)
    return _ADMIT
