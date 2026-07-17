"""Mechanical content classifier for passive answer-memory.

`is_coding_answer` decides whether a captured session answer is *purely*
coding work — a code dump, a diff, a shell transcript — as opposed to
knowledge worth recalling later, including decisions *about* code. It is the
capture-side analogue of the claude.ai memory system's "never store generic
technical questions" rule, and follows `secrets.mask_secrets`'s discipline:
curated, high-precision signals, no LLM and no entropy/ML.

Deliberately conservative. It biases toward *keep* (returns False) whenever the
coding signal is weak or any durable/decision signal is present, because
wrongly dropping a decision costs more than keeping a stray code answer.

Config: gated by ``capture.skip_coding`` (default false) in ``.vouch/config.yaml``.
"""

from __future__ import annotations

import re

# Minimum coding-dominance (0..1) before an answer is even a skip candidate.
_CODING_THRESHOLD = 0.6

# A fenced ```code``` block, backticks included.
_FENCE = re.compile(r"```.*?```", re.DOTALL)

# De-fenced lines that still read as source rather than prose.
_CODE_LINE = re.compile(
    r"""^\s*(
        (def|class|import|from|return|async|await|function|const|let|var|
         public|private|package|func|fn|impl|struct|enum|\#include)\b
        | [\w.\[\]]+\s*=\s*\S        # assignment: x = ...
        | .*[{}]\s*$                 # trailing brace
        | .*;\s*$                    # trailing semicolon
        | \$\s                       # shell prompt
    )""",
    re.VERBOSE,
)

# Real diff/patch headers (not markdown "- "/"+ " bullets).
_DIFF = re.compile(r"(?m)^(@@ |\+\+\+ |--- |diff --git )")

# Shell command markers.
_SHELL = re.compile(
    r"(?m)(^\s*\$\s)|\b(pip install|npm |yarn |git |sudo |cargo |make )\b"
)

# Rationale / decision language. Presence anywhere forces keep.
_DURABLE = re.compile(
    r"""(?ix)\b(
        decid | chose | choose | chosen | instead\ of | because | the\ reason
        | reason\ is | trade-?off | we\ should | going\ with | gotcha | lesson
        | prefer | avoid | so\ that | in\ order\ to | rather\ than | turns\ out
        | note\ that | the\ point\ is
    )\b"""
)


def _coding_score(text: str) -> float:
    """Fraction (0..1) of ``text`` that reads as code — max of several signals."""
    if not text.strip():
        return 0.0
    total = len(text)
    fenced = sum(len(m.group(0)) for m in _FENCE.finditer(text))
    fence_ratio = fenced / total if total else 0.0

    defenced = _FENCE.sub("", text)
    lines = [ln for ln in defenced.splitlines() if ln.strip()]
    code_lines = sum(1 for ln in lines if _CODE_LINE.match(ln))
    code_line_ratio = code_lines / len(lines) if lines else 0.0

    score = max(fence_ratio, code_line_ratio)
    if _DIFF.search(text):
        score = max(score, 0.75)
    if len(_SHELL.findall(text)) >= 2:
        score = max(score, 0.7)
    return score


def is_coding_answer(question: str, answer: str) -> bool:
    """True when ``answer`` is coding-dominant AND carries no durable signal."""
    if _DURABLE.search(question) or _DURABLE.search(answer):
        return False
    return _coding_score(answer) >= _CODING_THRESHOLD
