# Coding-Content Skip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, mechanical (no-LLM) skip rule to vouch's passive answer-memory path that drops purely-coding session answers while keeping durable/decision knowledge.

**Architecture:** A new pure classifier `capture_filters.is_coding_answer(question, answer) -> bool` scores an answer's coding-dominance from mechanical signals (fenced-code ratio, code-line fraction, diff/shell markers) with a durable/decision-language override that forces keep. `capture_answer` gains a config-gated guard (`capture.skip_coding`, default off) that returns the existing `_answer_skip(..., "coding-content")` when the classifier fires. The dev-activity observation path and the review gate are untouched.

**Tech Stack:** Python 3, stdlib `re`, pytest. No new dependencies.

## Global Constraints

- **No LLM in the capture path.** Classifier is pure, mechanical, stdlib-only — same discipline as `secrets.mask_secrets`. Verbatim from spec.
- **Reduce-only.** The rule may only make `capture_answer` skip. It never writes, never approves, never calls `proposals.approve`. The review gate is untouched.
- **Default off.** `CaptureConfig.skip_coding` defaults `False`; existing behaviour is unchanged unless a user opts in via `.vouch/config.yaml`.
- **Bias to keep.** When the coding signal is weak or any durable signal is present, return `False` (keep). False-negatives are cheap; wrongly dropping a decision is not.
- **CI gate (must pass):** `python -m pytest tests/ -q --ignore=tests/embeddings`, `python -m mypy src`, `python -m ruff check src tests`. `mypy` runs on `src` only.
- **Commit hygiene:** conventional commits, lowercase body, **no `Co-Authored-By` trailer**. Stage files by name — never `git add -A`. Commit-message hook rejects heredocs/`-m`; write the message to a scratch file and use `git commit -F`.
- **No new `kb.*` method** — internal config + filter only, so the four-site capability registration and `test_capabilities` do not apply.

---

### Task 1: `is_coding_answer` classifier

**Files:**
- Create: `src/vouch/capture_filters.py`
- Test: `tests/test_capture_filters.py`

**Interfaces:**
- Consumes: nothing (stdlib `re` only).
- Produces: `is_coding_answer(question: str, answer: str) -> bool` — True iff `answer` is coding-dominant (score ≥ threshold) AND neither `question` nor `answer` contains durable/decision language. Also a private `_coding_score(text: str) -> float` (0..1).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capture_filters.py`:

```python
"""Mechanical coding-content classifier for passive answer-memory."""

from __future__ import annotations

from vouch.capture_filters import is_coding_answer


def test_pure_code_block_is_coding() -> None:
    answer = (
        "```python\n"
        "def fib(n):\n"
        "    a, b = 0, 1\n"
        "    for _ in range(n):\n"
        "        a, b = b, a + b\n"
        "    return a\n"
        "```"
    )
    assert is_coding_answer("write fib", answer) is True


def test_unified_diff_is_coding() -> None:
    answer = (
        "diff --git a/x.py b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-old = 1\n"
        "+new = 2\n"
        " unchanged\n"
    )
    assert is_coding_answer("apply patch", answer) is True


def test_shell_transcript_is_coding() -> None:
    answer = (
        "$ pip install vouch-kb\n"
        "Successfully installed vouch-kb-1.0.0\n"
        "$ vouch init\n"
        "Initialized empty kb\n"
    )
    assert is_coding_answer("how to install", answer) is True


def test_decision_about_code_is_kept() -> None:
    answer = (
        "We chose an append-only jsonl log instead of sqlite because "
        "plaintext diffs in pull requests are the whole point.\n"
        "```python\nlog.append(event)\n```"
    )
    assert is_coding_answer("why jsonl?", answer) is False


def test_code_with_surrounding_rationale_is_kept() -> None:
    answer = (
        "The retry wrapper matters most when the network is flaky and a "
        "single dropped packet would otherwise fail the whole ingest. It "
        "keeps the fetch loop resilient without changing call sites, which "
        "is how every fetch path funnels through one helper today.\n"
        "```python\nfetch(url)\n```"
    )
    assert is_coding_answer("explain retries", answer) is False


def test_plain_prose_is_kept() -> None:
    answer = (
        "Vouch is a knowledge base where every write goes through a review "
        "gate. That invariant is the whole design; files on disk and the "
        "audit log are downstream of it."
    )
    assert is_coding_answer("what is vouch", answer) is False


def test_empty_answer_is_kept() -> None:
    assert is_coding_answer("", "") is False
    assert is_coding_answer("q", "   \n  ") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /tmp/vouch-coding-skip-wt && .venv/bin/python -m pytest tests/test_capture_filters.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vouch.capture_filters'`

- [ ] **Step 3: Write the classifier**

Create `src/vouch/capture_filters.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/vouch-coding-skip-wt && .venv/bin/python -m pytest tests/test_capture_filters.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Lint + type-check the new module**

Run: `cd /tmp/vouch-coding-skip-wt && .venv/bin/python -m ruff check src/vouch/capture_filters.py && .venv/bin/python -m mypy src/vouch/capture_filters.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
cd /tmp/vouch-coding-skip-wt
git add src/vouch/capture_filters.py tests/test_capture_filters.py
printf '%s\n' 'feat(capture): add mechanical coding-content classifier' '' 'is_coding_answer scores an answer'"'"'s coding-dominance from fenced-code' 'ratio, code-line fraction, and diff/shell markers, with a durable/' 'decision-language override that forces keep. pure, no llm, biased to' 'keep. not yet wired into capture_answer.' > /tmp/cc-msg.txt
git commit -F /tmp/cc-msg.txt
```

---

### Task 2: config field + wire the guard into `capture_answer`

**Files:**
- Modify: `src/vouch/capture.py` (imports; `CaptureConfig`; `load_config`; `capture_answer`)
- Test: `tests/test_capture_answer.py` (append three tests)

**Interfaces:**
- Consumes: `capture_filters.is_coding_answer` (Task 1); existing `_answer_skip(session_id, reason)`; `CaptureConfig`; `load_config`.
- Produces: new config field `CaptureConfig.skip_coding: bool = False`; new skip reason string `"coding-content"` returned from `capture_answer`.

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_capture_answer.py`:

```python
# --- coding-content skip -------------------------------------------------

CODING_ANSWER = (
    "```python\n"
    "def fib(n: int) -> int:\n"
    "    a, b = 0, 1\n"
    "    for _ in range(n):\n"
    "        a, b = b, a + b\n"
    "    return a\n"
    "\n"
    "def fact(n: int) -> int:\n"
    "    total = 1\n"
    "    for i in range(2, n + 1):\n"
    "        total *= i\n"
    "    return total\n"
    "```"
)  # purely fenced code, > 160 chars so it clears the too-short gate


def test_capture_answer_skips_coding_when_enabled(store: KBStore, tmp_path: Path) -> None:
    store.config_path.write_text("capture:\n  skip_coding: true\n", encoding="utf-8")
    tp = _transcript(
        tmp_path, [_user("write fib and factorial"), _assistant(CODING_ANSWER)]
    )
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["captured"] is False
    assert res["skipped"] == "coding-content"
    # nothing durable was written and no source was ingested.
    assert cap.pending_count(store) == 0
    assert list(store.list_claims()) == []


def test_capture_answer_keeps_coding_when_default(store: KBStore, tmp_path: Path) -> None:
    # regression guard: default config (skip_coding off) is unchanged.
    tp = _transcript(
        tmp_path, [_user("write fib and factorial"), _assistant(CODING_ANSWER)]
    )
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["skipped"] != "coding-content"
    assert res["captured"] is True


def test_capture_answer_keeps_decision_about_code(store: KBStore, tmp_path: Path) -> None:
    # durable override: skip_coding on, but a decision *about* code is kept.
    store.config_path.write_text(
        "capture:\n  skip_coding: true\nreview:\n  auto_approve_on_receipt: true\n",
        encoding="utf-8",
    )
    decision = (
        "We chose an append-only jsonl audit log instead of a sql table "
        "because plaintext diffs in pull requests are the whole point, and a "
        "binary store would break that review property for every write."
    )
    tp = _transcript(
        tmp_path, [_user("why jsonl for the audit log?"), _assistant(decision)]
    )
    res = cap.capture_answer(store, "sess-1", tp)
    assert res["skipped"] != "coding-content"
    assert res["captured"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /tmp/vouch-coding-skip-wt && .venv/bin/python -m pytest tests/test_capture_answer.py -q -k "coding or decision_about"`
Expected: FAIL — `test_capture_answer_skips_coding_when_enabled` fails (returns `captured=True`, no `"coding-content"` skip) because the guard doesn't exist yet.

- [ ] **Step 3: Add the config field**

In `src/vouch/capture.py`, add the default constant near the other capture defaults (after `DEFAULT_DEDUP_WINDOW_SECONDS = 60.0`):

```python
DEFAULT_SKIP_CODING = False
```

Add the field to `CaptureConfig` (the frozen dataclass):

```python
@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool = DEFAULT_ENABLED
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS
    skip_coding: bool = DEFAULT_SKIP_CODING
```

Parse it in `load_config` — add this line inside the returned `CaptureConfig(...)` call, alongside the existing fields:

```python
        skip_coding=bool(raw.get("skip_coding", DEFAULT_SKIP_CODING)),
```

- [ ] **Step 4: Import the classifier and wire the guard**

In `src/vouch/capture.py`, add the import next to `from .secrets import mask_secrets`:

```python
from .capture_filters import is_coding_answer
```

In `capture_answer`, immediately after the `answer-too-short` check, add the guard (before the content is hashed / sourced):

```python
    if len(answer) < min_answer_chars:
        return _answer_skip(session_id, "answer-too-short")
    if cfg.skip_coding and is_coding_answer(question, answer):
        return _answer_skip(session_id, "coding-content")
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd /tmp/vouch-coding-skip-wt && .venv/bin/python -m pytest tests/test_capture_answer.py -q -k "coding or decision_about"`
Expected: PASS (3 passed)

- [ ] **Step 6: Run the full capture suites (no regressions)**

Run: `cd /tmp/vouch-coding-skip-wt && .venv/bin/python -m pytest tests/test_capture.py tests/test_capture_answer.py tests/test_capture_filters.py -q`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
cd /tmp/vouch-coding-skip-wt
git add src/vouch/capture.py tests/test_capture_answer.py
printf '%s\n' 'feat(capture): gate answer-memory on opt-in coding-content skip' '' 'capture.skip_coding (default false) drops purely-coding session answers' 'from passive answer-memory while keeping durable/decision knowledge.' 'reduce-only: the guard returns the existing _answer_skip path and never' 'touches the review gate. the activity-log capture path is unchanged.' > /tmp/cc-msg2.txt
git commit -F /tmp/cc-msg2.txt
```

---

### Task 3: full gate + push

**Files:** none (verification + push only).

- [ ] **Step 1: Run the exact CI gate**

Run:
```bash
cd /tmp/vouch-coding-skip-wt
.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src tests
```
Expected: all three green. If `.venv` is absent in the worktree, create it: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev,web]'`.

- [ ] **Step 2: Confirm the commits are scoped and clean**

Run: `cd /tmp/vouch-coding-skip-wt && git log --oneline origin/test..HEAD && git diff --stat origin/test..HEAD`
Expected: three commits (spec, classifier, wiring); diff touches only `docs/superpowers/{specs,plans}/`, `src/vouch/capture_filters.py`, `src/vouch/capture.py`, `tests/test_capture_filters.py`, `tests/test_capture_answer.py`. No `Co-Authored-By` trailer in any commit.

- [ ] **Step 3: Push the branch**

Run: `cd /tmp/vouch-coding-skip-wt && git push -u origin feat/capture-coding-skip`
Expected: branch pushed; PR can target `test`.

---

## Self-Review

**Spec coverage:**
- classifier (`is_coding_answer`, mechanical, durable override, bias-to-keep) → Task 1 ✓
- `CaptureConfig.skip_coding` default off + `load_config` parse → Task 2 Step 3 ✓
- guard in `capture_answer` after `answer-too-short`, reuses `_answer_skip("coding-content")` → Task 2 Step 4 ✓
- activity-log path untouched → not modified anywhere ✓
- review gate untouched (reduce-only) → guard returns before `put_source`/`approve` ✓
- config documented → module docstring (Task 1) + committed spec ✓
- tests: pure-code/diff/shell → True; decision/rationale/prose/empty → False; integration skip + regression + durable override → Tasks 1 & 2 ✓
- out-of-scope (skip_topics, recall-side, user-editable exclusions) → not built ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `is_coding_answer(question: str, answer: str) -> bool` and `_coding_score(text: str) -> float` are used identically in Task 1 (definition + tests) and Task 2 (import + call). `CaptureConfig.skip_coding` / `DEFAULT_SKIP_CODING` names match between the dataclass, `load_config`, and the guard. Skip reason `"coding-content"` matches between the guard and the assertions.
