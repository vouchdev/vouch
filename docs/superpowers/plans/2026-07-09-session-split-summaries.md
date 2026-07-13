# Session-Split Summaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a session's captured activity is large, summarize it with an LLM into several topical `type: session` page proposals instead of one mechanical rollup — host-neutrally, with the review gate intact.

**Architecture:** A new host-blind module `session_split.py` owns the buffer→pages pipeline (size gate → mechanical rollup *or* LLM topical split → PENDING proposals). `capture.finalize` becomes a thin wrapper that resolves the Claude-Code transcript intent and delegates. Shared LLM subprocess/parse plumbing is extracted from `compile.py` into `llm_draft.py`. A new `kb.summarize_session` method lets hosts without shell hooks trigger it.

**Tech Stack:** Python 3, pydantic models, click CLI, pytest, mypy, ruff. LLM is a deployment-configured shell command (`capture.split.llm_cmd`, defaulting to `compile.llm_cmd`).

## Global Constraints

- Conventional commits, lowercase body, **no `Co-Authored-By` trailer** (checked in review).
- The commit hook rejects `git -m` and heredocs — write the message to `/tmp/vouch-commit-msg.txt` via `printf` and use `git commit -F`.
- Stage files **by name**; never `git add -A` (leaks `.claude/`, `webapp/`, WIP `.vouch/`).
- CI gate is exactly: `pytest tests/ -q --ignore=tests/embeddings`, `mypy src`, `ruff check src tests`. `make check` runs all three.
- **The review gate is load-bearing:** every produced page is filed via `proposals.propose_page` as a PENDING proposal. `approve()` is NEVER called in this feature.
- Split pages are always `page_type="session"` (forced in code) — never `concept`/`workflow`/`decision`. Sessions are feedstock, not compiled wiki pages.
- Work on branch `test` (current). Do not sweep the pre-existing WIP working tree into commits.

---

## File Structure

- **Create** `src/vouch/llm_draft.py` — shared `run_llm` + `parse_drafts` + fence-strip + `LLMDraftError`.
- **Create** `src/vouch/session_split.py` — host-blind `summarize()` core, `SplitConfig`, prompt builder, draft filer, audit.
- **Create** `tests/test_llm_draft.py`, `tests/test_session_split.py`.
- **Modify** `src/vouch/compile.py` — `run_llm`/`parse_drafts` become thin wrappers over `llm_draft`; drop now-unused imports.
- **Modify** `src/vouch/capture.py` — `finalize()` delegates to `session_split.summarize`; gains a `mode` param.
- **Modify** `src/vouch/capabilities.py` — add `"kb.summarize_session"` to `METHODS`.
- **Modify** `src/vouch/server.py` — add `kb_summarize_session` MCP tool.
- **Modify** `src/vouch/jsonl_server.py` — add `_h_summarize_session` + `HANDLERS` entry.
- **Modify** `src/vouch/cli.py` — `--split/--no-split` on `capture finalize`; new `capture summarize` command.
- **Modify** `src/vouch/storage.py` — add `capture.split` defaults to `_starter_config`.

---

## Task 1: Extract shared LLM plumbing into `llm_draft.py`

**Files:**
- Create: `src/vouch/llm_draft.py`
- Create: `tests/test_llm_draft.py`
- Modify: `src/vouch/compile.py` (imports; `run_llm`, `parse_drafts` bodies; remove local `_FENCE_RE`)

**Interfaces:**
- Produces: `llm_draft.run_llm(llm_cmd: str, prompt: str, *, timeout_seconds: float, label: str = "llm_cmd") -> str`; `llm_draft.parse_drafts(raw: str, *, noun: str = "page") -> list[dict[str, Any]]`; `llm_draft.LLMDraftError(Exception)`; `llm_draft._FENCE_RE`.
- Consumes (in compile): nothing new; `compile.CompileError` still wraps failures so compile's public error contract and messages are unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm_draft.py`:

```python
"""Shared LLM drafting plumbing used by compile and session_split."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import llm_draft
from vouch.llm_draft import LLMDraftError, parse_drafts, run_llm


def _stub(tmp_path: Path, payload: str) -> str:
    out = tmp_path / "out.txt"
    out.write_text(payload, encoding="utf-8")
    return f"cat {out}"


def test_run_llm_returns_stdout(tmp_path: Path) -> None:
    cmd = _stub(tmp_path, '[{"title": "x", "body": "y"}]')
    assert run_llm(cmd, "prompt", timeout_seconds=10.0).strip().startswith("[")


def test_run_llm_nonzero_raises_with_label(tmp_path: Path) -> None:
    with pytest.raises(LLMDraftError, match="capture.split.llm_cmd failed"):
        run_llm("false", "p", timeout_seconds=10.0, label="capture.split.llm_cmd")


def test_run_llm_timeout_raises(tmp_path: Path) -> None:
    with pytest.raises(LLMDraftError, match="timed out"):
        run_llm("sleep 5", "p", timeout_seconds=0.2)


def test_parse_drafts_strips_fence() -> None:
    raw = '```json\n[{"title": "a", "body": "b"}]\n```'
    assert parse_drafts(raw) == [{"title": "a", "body": "b"}]


def test_parse_drafts_bad_json_raises() -> None:
    with pytest.raises(LLMDraftError, match="not valid JSON"):
        parse_drafts("not json")


def test_parse_drafts_non_list_raises() -> None:
    with pytest.raises(LLMDraftError, match="must be a JSON array"):
        parse_drafts('{"title": "a"}')


def test_parse_drafts_non_dict_element_raises() -> None:
    with pytest.raises(LLMDraftError, match="array of page objects"):
        parse_drafts('["just a string"]')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_llm_draft.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vouch.llm_draft'`.

- [ ] **Step 3: Create `src/vouch/llm_draft.py`**

```python
"""Shared LLM drafting plumbing for the review-gated compilers.

Both `compile.py` (approved claims -> topic pages) and `session_split.py`
(session observations -> topical session pages) hand a deployment-configured
LLM command a prompt on stdin and parse a JSON array of drafts back. This
module is the shared subprocess + parse layer; each caller keeps its own
domain validation (compile verifies claim citations; session_split forces the
session page type).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from typing import Any

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n```$")


class LLMDraftError(Exception):
    """The LLM command could not run, or returned unusable output."""


def run_llm(
    llm_cmd: str,
    prompt: str,
    *,
    timeout_seconds: float,
    label: str = "llm_cmd",
) -> str:
    """Run `llm_cmd` with `prompt` on stdin, in a throwaway temp cwd.

    `label` names the command in error messages so callers keep their own
    config-key wording (e.g. "compile.llm_cmd"). Runs in a temp dir so an LLM
    CLI that discovers per-project hooks/MCP from its cwd does not fire this
    project's own pipeline while summarizing it. UTF-8 is forced on both pipe
    directions — the default follows the locale (Latin-1 on some hosts), which
    would crash on the first em-dash; `errors="replace"` surfaces a stray
    invalid byte as a visible replacement char in review, not an exception.
    """
    with tempfile.TemporaryDirectory(prefix="vouch-llm-") as tmp:
        try:
            proc = subprocess.run(
                llm_cmd, shell=True, cwd=tmp,
                input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMDraftError(f"{label} timed out after {timeout_seconds:.0f}s") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise LLMDraftError(f"{label} failed ({proc.returncode}): {detail}")
    return proc.stdout


def parse_drafts(raw: str, *, noun: str = "page") -> list[dict[str, Any]]:
    """Parse LLM stdout into a list of draft dicts.

    Strips a single markdown code fence if present. `noun` tunes error wording
    ("page" -> "JSON array of pages"). Raises LLMDraftError on any shape
    failure so callers can surface it as a clean, caller-visible message.
    """
    text = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMDraftError(f"compiler output is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise LLMDraftError(f"compiler output must be a JSON array of {noun}s")
    for item in data:
        if not isinstance(item, dict):
            raise LLMDraftError(
                f"compiler output must be a JSON array of {noun} objects, "
                f"got element of type {type(item).__name__}"
            )
    return list(data)
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_llm_draft.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Rewrite compile.py's `run_llm`/`parse_drafts` as wrappers**

In `src/vouch/compile.py`, replace the entire bodies of `run_llm` (currently ~lines 182-210) and `parse_drafts` (~lines 213-230) and delete the module-level `_FENCE_RE` (~line 53) with:

```python
def run_llm(llm_cmd: str, prompt: str, *, timeout_seconds: float) -> str:
    """Run the configured LLM command with the prompt on stdin.

    Thin wrapper over `llm_draft.run_llm`, translating its error into the
    `CompileError` compile callers already handle, and keeping the
    "compile.llm_cmd …" wording in messages.
    """
    try:
        return llm_draft.run_llm(
            llm_cmd, prompt, timeout_seconds=timeout_seconds,
            label="compile.llm_cmd",
        )
    except llm_draft.LLMDraftError as e:
        raise CompileError(str(e)) from e


def parse_drafts(raw: str) -> list[dict[str, Any]]:
    try:
        return llm_draft.parse_drafts(raw, noun="page")
    except llm_draft.LLMDraftError as e:
        raise CompileError(str(e)) from e
```

Then fix the imports at the top of `compile.py`: add `from . import llm_draft` next to the other `from . import …` lines, and **remove** the now-unused `import json`, `import subprocess`, `import tempfile` (keep `import re` — it is still used by `_WIKILINK_RE`/`_CLAIM_MARKER_RE`).

- [ ] **Step 6: Verify compile tests + lint still pass (unchanged behavior)**

Run: `.venv/bin/python -m pytest tests/test_compile.py tests/test_llm_draft.py -q && .venv/bin/python -m ruff check src/vouch/compile.py src/vouch/llm_draft.py && .venv/bin/python -m mypy src/vouch/compile.py src/vouch/llm_draft.py`
Expected: all PASS. `test_compile.py`'s error-message assertions still match because the wrappers reproduce the exact strings.

- [ ] **Step 7: Commit**

```bash
printf '%s\n' 'refactor(compile): extract shared llm drafting into llm_draft' > /tmp/vouch-commit-msg.txt
git add src/vouch/llm_draft.py src/vouch/compile.py tests/test_llm_draft.py
git commit -F /tmp/vouch-commit-msg.txt
```

---

## Task 2: `SplitConfig` loaded from `capture.split`

**Files:**
- Create: `src/vouch/session_split.py` (config portion only)
- Create: `tests/test_session_split.py` (config tests only)

**Interfaces:**
- Produces: `session_split.SplitConfig` dataclass with fields `enabled: bool=True`, `llm_cmd: str|None=None`, `threshold_observations: int=40`, `max_pages: int=6`, `timeout_seconds: float=180.0`, `max_input_chars: int=60000`; `session_split.load_split_config(store: KBStore) -> SplitConfig`; `session_split.SplitConfigError(Exception)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_split.py`:

```python
"""Host-blind session summarization: size gate, mechanical rollup, LLM split."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import session_split
from vouch.session_split import SplitConfig, load_split_config
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_split_config_defaults(store: KBStore) -> None:
    cfg = load_split_config(store)
    assert cfg == SplitConfig()
    assert cfg.threshold_observations == 40
    assert cfg.max_pages == 6
    assert cfg.enabled is True


def test_split_config_reads_override(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  split:\n    threshold_observations: 5\n    max_pages: 2\n"
        "    llm_cmd: \"cat /dev/null\"\n",
        encoding="utf-8",
    )
    cfg = load_split_config(store)
    assert cfg.threshold_observations == 5
    assert cfg.max_pages == 2
    assert cfg.llm_cmd == "cat /dev/null"


def test_split_config_malformed_yaml_falls_back(store: KBStore) -> None:
    store.config_path.write_text("capture:\n  split:\n  - not-a-mapping\n", encoding="utf-8")
    assert load_split_config(store) == SplitConfig()


def test_split_config_typo_coerces_to_default(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  split:\n    max_pages: six\n", encoding="utf-8"
    )
    assert load_split_config(store).max_pages == 6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_session_split.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vouch.session_split'`.

- [ ] **Step 3: Create `src/vouch/session_split.py` (config only for now)**

```python
"""Summarize a session's observation buffer into review-gated pages.

Host-blind: reads only the normalized observation buffer
(`.vouch/captures/<id>.jsonl`) that every host adapter writes via
`capture.observe`, never a host transcript. Small sessions get one mechanical
rollup page (reusing `capture.build_summary_body`); large sessions get an LLM
topical split into several `type: session` pages. Every page is a PENDING
proposal — `approve()` is never called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import yaml

from .storage import KBStore

logger = logging.getLogger(__name__)

SPLIT_ACTOR = "session-split"

DEFAULT_THRESHOLD_OBSERVATIONS = 40
DEFAULT_MAX_PAGES = 6
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_INPUT_CHARS = 60000


class SplitConfigError(Exception):
    """The split cannot run (no resolvable llm_cmd)."""


@dataclass(frozen=True)
class SplitConfig:
    enabled: bool = True
    llm_cmd: str | None = None
    threshold_observations: int = DEFAULT_THRESHOLD_OBSERVATIONS
    max_pages: int = DEFAULT_MAX_PAGES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS


def _coerce(value: Any, default: Any, cast: Any) -> Any:
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def load_split_config(store: KBStore) -> SplitConfig:
    """Read `capture.split` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return SplitConfig()
    if not isinstance(loaded, dict):
        return SplitConfig()
    cap = loaded.get("capture")
    raw = cap.get("split") if isinstance(cap, dict) else None
    if not isinstance(raw, dict):
        return SplitConfig()
    llm_cmd = raw.get("llm_cmd")
    return SplitConfig(
        enabled=bool(raw.get("enabled", True)),
        llm_cmd=str(llm_cmd) if llm_cmd else None,
        threshold_observations=_coerce(
            raw.get("threshold_observations", DEFAULT_THRESHOLD_OBSERVATIONS),
            DEFAULT_THRESHOLD_OBSERVATIONS, int),
        max_pages=_coerce(raw.get("max_pages", DEFAULT_MAX_PAGES), DEFAULT_MAX_PAGES, int),
        timeout_seconds=_coerce(
            raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            DEFAULT_TIMEOUT_SECONDS, float),
        max_input_chars=_coerce(
            raw.get("max_input_chars", DEFAULT_MAX_INPUT_CHARS),
            DEFAULT_MAX_INPUT_CHARS, int),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_session_split.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
printf '%s\n' 'feat(session-split): add SplitConfig loaded from capture.split' > /tmp/vouch-commit-msg.txt
git add src/vouch/session_split.py tests/test_session_split.py
git commit -F /tmp/vouch-commit-msg.txt
```

---

## Task 3: `summarize()` mechanical pipeline + `finalize` delegation (behavior-preserving)

**Files:**
- Modify: `src/vouch/session_split.py` (add `summarize`, `_propose_mechanical`)
- Modify: `src/vouch/capture.py` (`finalize` delegates; add `mode` param)
- Modify: `tests/test_session_split.py` (add pipeline tests)

**Interfaces:**
- Produces: `session_split.summarize(store, session_id, *, intent=None, cwd=None, project=None, generated_at=None, mode="auto", config=None) -> dict[str, Any]`. Return dict keys: `captured:int`, `summary_proposal_id:str|None`, `summary_proposal_ids:list[str]`, `mode:str`, and `skipped:str` on skip paths.
- Consumes: `capture.load_config`, `capture.buffer_path`, `capture._read_observations`, `capture._git_changes`, `capture.build_summary_body`, `capture.CAPTURE_ACTOR`, `capture.CAPTURE_PAGE_TYPE`, `capture.CaptureConfig`; `proposals.propose_page`.
- `capture.finalize` keeps its signature and gains `mode: str = "auto"`, delegating to `summarize` via a deferred import (breaks the capture↔session_split cycle).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_split.py`:

```python
def _observe(store: KBStore, sid: str, n: int, tool: str = "Edit") -> None:
    from vouch import capture
    for i in range(n):
        capture.observe(store, sid, tool=tool, summary=f"{tool} file{i}.py", now=float(i))


def test_below_min_skips_and_deletes_buffer(store: KBStore) -> None:
    from vouch import capture
    capture.observe(store, "s1", tool="Edit", summary="one", now=1.0)
    res = session_split.summarize(store, "s1")
    assert res["skipped"] == "below-min"
    assert res["summary_proposal_ids"] == []
    assert not capture.buffer_path(store, "s1").exists()


def test_disabled_returns_skip(store: KBStore) -> None:
    from vouch import capture
    _observe(store, "s1", 5)
    cfg = capture.CaptureConfig(enabled=False)
    res = session_split.summarize(store, "s1", config=cfg)
    assert res["skipped"] == "disabled"


def test_mechanical_single_page_below_threshold(store: KBStore) -> None:
    from vouch.models import ProposalStatus
    _observe(store, "s1", 5)  # >= min (3), < threshold (40)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "mechanical"
    assert len(res["summary_proposal_ids"]) == 1
    assert res["summary_proposal_id"] == res["summary_proposal_ids"][0]
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].payload["type"] == "session"


def test_finalize_still_returns_summary_proposal_id(store: KBStore) -> None:
    from vouch import capture
    _observe(store, "s1", 5)
    res = capture.finalize(store, "s1", cwd=None, generated_at="2026-07-09T00:00:00Z")
    assert "summary_proposal_id" in res
    assert res["summary_proposal_id"] is not None
    assert res["mode"] == "mechanical"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_session_split.py -k "below_min or disabled or mechanical or finalize_still" -q`
Expected: FAIL — `AttributeError: module 'vouch.session_split' has no attribute 'summarize'`.

- [ ] **Step 3: Add `summarize` + `_propose_mechanical` to `session_split.py`**

Add these imports to the top of `session_split.py` (next to the existing ones):

```python
from pathlib import Path

from . import capture
from .proposals import propose_page
```

Append to `session_split.py`:

```python
def summarize(
    store: KBStore,
    session_id: str,
    *,
    intent: str | None = None,
    cwd: Path | None = None,
    project: str | None = None,
    generated_at: str | None = None,
    mode: str = "auto",
    config: capture.CaptureConfig | None = None,
) -> dict[str, Any]:
    """Roll a session buffer into PENDING page proposals. Never approves.

    `mode`: "auto" (size gate decides), "split" (force LLM), or "mechanical"
    (force the single rollup). The buffer is deleted only after a page is
    filed (or an explicit below-min skip), so a crash mid-run leaves it intact
    for the next `finalize-all` sweep to retry.
    """
    cfg = config or capture.load_config(store)
    path = capture.buffer_path(store, session_id)
    observations = capture._read_observations(path)
    if not cfg.enabled:
        return {"captured": len(observations), "summary_proposal_id": None,
                "summary_proposal_ids": [], "mode": "skipped", "skipped": "disabled"}
    if cwd is not None:
        changed_files, git_stat = capture._git_changes(cwd)
    else:
        changed_files, git_stat = [], ""
    total = len(observations) + len(changed_files)
    if total < cfg.min_observations:
        if path.exists():
            path.unlink()
        return {"captured": total, "summary_proposal_id": None,
                "summary_proposal_ids": [], "mode": "skipped", "skipped": "below-min"}

    # Task 4 inserts the LLM split branch here.

    pid = _propose_mechanical(
        store, session_id, observations, changed_files, git_stat,
        project=project, generated_at=generated_at, intent=intent,
    )
    if path.exists():
        path.unlink()
    return {"captured": total, "summary_proposal_id": pid,
            "summary_proposal_ids": [pid], "mode": "mechanical"}


def _propose_mechanical(
    store: KBStore,
    session_id: str,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    project: str | None,
    generated_at: str | None,
    intent: str | None,
) -> str:
    """File the single mechanical rollup page, exactly as capture did before."""
    title, body = capture.build_summary_body(
        session_id, observations, changed_files, git_stat,
        project=project, generated_at=generated_at, first_prompt=intent,
    )
    proposal = propose_page(
        store, title=title, body=body,
        page_type=capture.CAPTURE_PAGE_TYPE,
        proposed_by=capture.CAPTURE_ACTOR,
        session_id=session_id,
        rationale="auto-captured session summary",
    )
    return proposal.id
```

- [ ] **Step 4: Make `capture.finalize` delegate**

In `src/vouch/capture.py`, replace the body of `finalize` (currently ~lines 315-369) with:

```python
def finalize(
    store: KBStore,
    session_id: str,
    *,
    cwd: Path | None = None,
    project: str | None = None,
    generated_at: str | None = None,
    transcript_path: Path | None = None,
    mode: str = "auto",
    config: CaptureConfig | None = None,
) -> dict[str, Any]:
    """Roll a session buffer into PENDING summary proposal(s). No approve().

    Claude-Code-facing wrapper: resolves the transcript's first user prompt
    (its one host-specific enricher) and delegates to the host-blind
    `session_split.summarize`. `mode` forwards "auto" | "split" | "mechanical".
    """
    from . import session_split  # deferred: breaks the capture<->session_split cycle
    intent = (
        first_user_prompt(transcript_path) if transcript_path is not None else None
    )
    return session_split.summarize(
        store, session_id, intent=intent, cwd=cwd, project=project,
        generated_at=generated_at, mode=mode, config=config,
    )
```

Leave `observe`, `summarize_tool`, `build_summary_body`, `first_user_prompt`, `finalize_all_except`, and the buffer helpers in place — `session_split` reuses them.

- [ ] **Step 5: Run tests + lint + types**

Run: `.venv/bin/python -m pytest tests/test_session_split.py tests/test_capture.py -q && .venv/bin/python -m mypy src/vouch/session_split.py src/vouch/capture.py && .venv/bin/python -m ruff check src/vouch/session_split.py src/vouch/capture.py`
Expected: all PASS. `test_capture.py` stays green because the mechanical path reproduces the old actor/type/rationale and return keys.

- [ ] **Step 6: Commit**

```bash
printf '%s\n' 'refactor(capture): route finalize through session_split.summarize' > /tmp/vouch-commit-msg.txt
git add src/vouch/session_split.py src/vouch/capture.py tests/test_session_split.py
git commit -F /tmp/vouch-commit-msg.txt
```

---

## Task 4: LLM topical split + fallback

**Files:**
- Modify: `src/vouch/session_split.py` (add split branch, `_propose_split`, `build_split_prompt`, `_file_drafts`, `_audit_split`)
- Modify: `tests/test_session_split.py` (add split tests)

**Interfaces:**
- Consumes: `llm_draft.run_llm`, `llm_draft.parse_drafts`, `llm_draft.LLMDraftError`; `compile._pending_page_names`, `compile.load_config` (for the `compile.llm_cmd` fallback); `proposals._slugify`; `audit.log_event`; `store.list_pages`.
- Produces: internal `_propose_split(...) -> tuple[list[str], list[dict[str, Any]], bool]`; `summarize` now returns `mode="split"` with `dropped`/`truncated` on success, or `mode="fallback"` when the LLM path is attempted but yields nothing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_split.py`:

```python
def _stub_llm(tmp_path: Path, drafts: list[dict]) -> str:
    out = tmp_path / "drafts.json"
    out.write_text(json.dumps(drafts), encoding="utf-8")
    return f"cat {out}"


def _config_with_split(store: KBStore, llm_cmd: str, threshold: int = 3, max_pages: int = 6) -> None:
    store.config_path.write_text(
        "capture:\n  split:\n"
        f"    threshold_observations: {threshold}\n"
        f"    max_pages: {max_pages}\n"
        f"    llm_cmd: \"{llm_cmd}\"\n",
        encoding="utf-8",
    )


def test_split_files_multiple_pending_session_pages(store: KBStore, tmp_path: Path) -> None:
    from vouch.models import ProposalStatus
    _observe(store, "s1", 5)
    cmd = _stub_llm(tmp_path, [
        {"title": "refactored the audit writer", "body": "one thread of work " * 10},
        {"title": "fixed the ci locale bug", "body": "another thread of work " * 10},
    ])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "split"
    assert len(res["summary_proposal_ids"]) == 2
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 2
    assert all(p.payload["type"] == "session" for p in pending)
    assert all(p.proposed_by == session_split.SPLIT_ACTOR for p in pending)
    assert store.list_pages() == []  # nothing durable — only proposed


def test_split_forces_session_type_even_if_llm_says_concept(store: KBStore, tmp_path: Path) -> None:
    _observe(store, "s1", 5)
    cmd = _stub_llm(tmp_path, [
        {"title": "a topic", "type": "concept", "body": "body " * 20},
    ])
    _config_with_split(store, cmd, threshold=3)
    session_split.summarize(store, "s1", mode="auto")
    from vouch.models import ProposalStatus
    assert store.list_proposals(ProposalStatus.PENDING)[0].payload["type"] == "session"


def test_no_llm_cmd_falls_back_to_mechanical(store: KBStore) -> None:
    _observe(store, "s1", 50)  # over default threshold 40
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "fallback"
    assert len(res["summary_proposal_ids"]) == 1


def test_junk_llm_output_falls_back(store: KBStore) -> None:
    _observe(store, "s1", 5)
    _config_with_split(store, "echo not-json", threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "fallback"
    assert len(res["summary_proposal_ids"]) == 1


def test_dedupe_drops_colliding_title(store: KBStore, tmp_path: Path) -> None:
    from vouch.proposals import approve, propose_page
    pr = propose_page(store, title="Existing Topic", body="b", page_type="concept", proposed_by="a")
    approve(store, pr.id, approved_by="human-B")
    _observe(store, "s1", 5)
    cmd = _stub_llm(tmp_path, [
        {"title": "Existing Topic", "body": "dup " * 20},
        {"title": "Fresh Topic", "body": "fresh " * 20},
    ])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert len(res["summary_proposal_ids"]) == 1
    assert any(d["reason"].startswith("title already") for d in res["dropped"])


def test_cap_enforced(store: KBStore, tmp_path: Path) -> None:
    _observe(store, "s1", 5)
    drafts = [{"title": f"topic {i}", "body": "x " * 20} for i in range(5)]
    cmd = _stub_llm(tmp_path, drafts)
    _config_with_split(store, cmd, threshold=3, max_pages=2)
    res = session_split.summarize(store, "s1", mode="auto")
    assert len(res["summary_proposal_ids"]) == 2
    assert len([d for d in res["dropped"] if "over max_pages" in d["reason"]]) == 3


def test_host_neutral_tool_names_do_not_crash(store: KBStore, tmp_path: Path) -> None:
    from vouch import capture
    for i, tool in enumerate(["fs.write", "shell.exec", "browser.open"]):
        capture.observe(store, "s1", tool=tool, summary=f"{tool} did thing {i}", now=float(i))
    capture.observe(store, "s1", tool="fs.write", summary="one more", now=9.0)
    cmd = _stub_llm(tmp_path, [{"title": "the work", "body": "did things " * 15}])
    _config_with_split(store, cmd, threshold=3)
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["mode"] == "split"


def test_truncation_flagged_when_over_budget(store: KBStore, tmp_path: Path) -> None:
    from vouch import capture
    for i in range(50):
        capture.observe(store, "s1", tool="Edit", summary="x" * 200, now=float(i))
    cmd = _stub_llm(tmp_path, [{"title": "t", "body": "b " * 20}])
    store.config_path.write_text(
        "capture:\n  split:\n    threshold_observations: 3\n"
        "    max_input_chars: 500\n"
        f"    llm_cmd: \"{cmd}\"\n",
        encoding="utf-8",
    )
    res = session_split.summarize(store, "s1", mode="auto")
    assert res["truncated"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_session_split.py -k "split_files or forces_session or no_llm or junk or dedupe or cap_enforced or host_neutral or truncation" -q`
Expected: FAIL — the split path does not exist yet, so `mode` is never `"split"`/`"fallback"` (currently these observation counts route to `mechanical`, and default-threshold cases assert `fallback`).

- [ ] **Step 3: Add the split branch + helpers to `session_split.py`**

Add imports to the top of `session_split.py`:

```python
from . import audit as audit_mod
from . import compile as compile_mod
from . import llm_draft
from .llm_draft import LLMDraftError
from .proposals import _slugify
```

Replace the `# Task 4 inserts the LLM split branch here.` marker in `summarize` with:

```python
    split_cfg = load_split_config(store)
    want_split = mode == "split" or (
        mode == "auto" and split_cfg.enabled and total >= split_cfg.threshold_observations
    )
    if mode != "mechanical" and want_split:
        try:
            ids, dropped, truncated = _propose_split(
                store, session_id, observations, changed_files, git_stat,
                intent=intent, split_cfg=split_cfg,
            )
            if ids:
                if path.exists():
                    path.unlink()
                return {"captured": total, "summary_proposal_id": ids[0],
                        "summary_proposal_ids": ids, "mode": "split",
                        "dropped": dropped, "truncated": truncated}
            logger.warning(
                "session_split: no valid drafts for %s; falling back to mechanical",
                session_id,
            )
        except (LLMDraftError, SplitConfigError) as e:
            logger.warning(
                "session_split: llm split failed for %s (%s); falling back", session_id, e
            )
```

And change the final mechanical return so a fallback is labeled distinctly. Replace the tail of `summarize` (the `_propose_mechanical` call and its return) with:

```python
    pid = _propose_mechanical(
        store, session_id, observations, changed_files, git_stat,
        project=project, generated_at=generated_at, intent=intent,
    )
    if path.exists():
        path.unlink()
    final_mode = "fallback" if (mode != "mechanical" and want_split) else "mechanical"
    return {"captured": total, "summary_proposal_id": pid,
            "summary_proposal_ids": [pid], "mode": final_mode}
```

Append the split helpers to `session_split.py`:

```python
def _propose_split(
    store: KBStore,
    session_id: str,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    intent: str | None,
    split_cfg: SplitConfig,
) -> tuple[list[str], list[dict[str, Any]], bool]:
    cmd = split_cfg.llm_cmd or compile_mod.load_config(store).llm_cmd
    if not cmd:
        raise SplitConfigError(
            "capture.split.llm_cmd is not configured (and compile.llm_cmd is unset)"
        )
    prompt, truncated = build_split_prompt(
        store, observations, changed_files, git_stat,
        intent=intent, max_pages=split_cfg.max_pages,
        max_input_chars=split_cfg.max_input_chars,
    )
    raw = llm_draft.run_llm(
        cmd, prompt, timeout_seconds=split_cfg.timeout_seconds,
        label="capture.split.llm_cmd",
    )
    drafts = llm_draft.parse_drafts(raw, noun="page")
    ids, dropped = _file_drafts(store, session_id, drafts, split_cfg.max_pages)
    _audit_split(store, session_id, ids, dropped, len(observations), truncated)
    return ids, dropped, truncated


def _render_obs(obs: dict[str, Any]) -> str:
    tool = str(obs.get("tool", "")).strip()
    summary = str(obs.get("summary", "")).strip()
    files = obs.get("files") or []
    line = f"[{tool}] {summary}" if tool else summary
    if files:
        line += f" (files: {', '.join(str(f) for f in files[:5])})"
    return line


def build_split_prompt(
    store: KBStore,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    intent: str | None,
    max_pages: int,
    max_input_chars: int,
) -> tuple[str, bool]:
    """Assemble the host-neutral topical-split prompt. Returns (prompt, truncated).

    `tool` labels are opaque — the model clusters on the `summary` prose, so any
    host's tool vocabulary works. If the rendered activity exceeds
    `max_input_chars`, keep the most-recent observations that fit and prepend an
    explicit elision note (no silent cap).
    """
    obs_lines = [_render_obs(o) for o in observations]
    truncated = False
    if sum(len(x) + 1 for x in obs_lines) > max_input_chars:
        truncated = True
        kept: list[str] = []
        size = 0
        for line in reversed(obs_lines):
            if size + len(line) + 1 > max_input_chars:
                break
            kept.append(line)
            size += len(line) + 1
        obs_lines = list(reversed(kept))
        elided = len(observations) - len(obs_lines)
        obs_lines.insert(0, f"(... {elided} older observations elided ...)")

    lines: list[str] = [
        "You are the session historian for this project's knowledge base. You",
        "summarize one work session into a small set of durable, human-readable",
        "session records — one per distinct thread of work.",
        "",
    ]
    if intent:
        lines += ["SESSION INTENT:", f"  {intent}", ""]
    lines += ["SESSION ACTIVITY (one line per observation, oldest first):"]
    lines += [f"- {line}" for line in obs_lines]
    lines += [""]
    if changed_files:
        lines += ["FILES CHANGED:"]
        lines += [f"- {f}" for f in changed_files[:50]]
        lines += [""]
    if git_stat:
        lines += ["GIT STAT:", "```", git_stat, "```", ""]

    pages = store.list_pages()
    pending = compile_mod._pending_page_names(store)
    taken = [f"- {p.title}" for p in pages] + [f"- {n} [pending]" for n in sorted(pending)]
    lines += ["TAKEN TOPICS (do NOT redraft any of these):"]
    lines += taken or ["- (none)"]
    lines += [
        "",
        "RULES",
        f"- Cluster the activity into at most {max_pages} coherent TOPICS —",
        "  distinct threads of work in this session. Draft one page per topic.",
        "- Each page needs a specific title (\"fixed the audit-log write race\",",
        "  not \"bug fixes\") and an 80-200 word markdown body summarizing that",
        "  thread of work.",
        "- These are session records, NOT wiki topic pages: do NOT add",
        "  [claim: id] markers, and do NOT invent facts beyond the activity shown.",
        "- Skip any topic already listed under TAKEN TOPICS.",
        "",
        "OUTPUT: print ONLY a JSON array, no code fences, no commentary.",
        "Each element: {\"title\": str, \"body\": str}",
    ]
    return "\n".join(lines), truncated


def _file_drafts(
    store: KBStore,
    session_id: str,
    drafts: list[dict[str, Any]],
    max_pages: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    existing = store.list_pages()
    taken = {p.title.strip().lower() for p in existing}
    taken |= {p.id.strip().lower() for p in existing}
    taken |= compile_mod._pending_page_names(store)
    ids: list[str] = []
    dropped: list[dict[str, Any]] = []
    for i, draft in enumerate(drafts):
        title = str(draft.get("title") or "").strip()
        body = str(draft.get("body") or "").strip()
        if not title:
            dropped.append({"title": f"draft {i}", "reason": "draft has no title"})
            continue
        if not body:
            dropped.append({"title": title, "reason": "draft has no body"})
            continue
        if len(ids) >= max_pages:
            dropped.append({"title": title, "reason": f"over max_pages={max_pages}"})
            continue
        if title.lower() in taken or _slugify(title) in taken:
            dropped.append({"title": title, "reason": "title already exists or is pending"})
            continue
        proposal = propose_page(
            store, title=title, body=body,
            page_type=capture.CAPTURE_PAGE_TYPE,  # "session" — forced, ignore any LLM type
            proposed_by=SPLIT_ACTOR,
            tags=["session", "split"],
            session_id=session_id,
            metadata={"session_id": session_id},
            rationale=f"llm topical split of session {session_id}",
        )
        ids.append(proposal.id)
        taken.add(title.lower())
        taken.add(_slugify(title))
    return ids, dropped


def _audit_split(
    store: KBStore,
    session_id: str,
    ids: list[str],
    dropped: list[dict[str, Any]],
    n_observations: int,
    truncated: bool,
) -> None:
    audit_mod.log_event(
        store.kb_dir, event="session.split", actor=SPLIT_ACTOR,
        object_ids=ids,
        data={"proposed": len(ids), "dropped": len(dropped),
              "observations": n_observations, "truncated": truncated},
    )
```

- [ ] **Step 4: Run the split tests**

Run: `.venv/bin/python -m pytest tests/test_session_split.py -q`
Expected: PASS (all config + pipeline + split tests).

- [ ] **Step 5: Lint + types + full capture/compile regression**

Run: `.venv/bin/python -m mypy src/vouch/session_split.py && .venv/bin/python -m ruff check src/vouch/session_split.py && .venv/bin/python -m pytest tests/test_capture.py tests/test_compile.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
printf '%s\n' 'feat(session-split): llm topical split for large sessions' > /tmp/vouch-commit-msg.txt
git add src/vouch/session_split.py tests/test_session_split.py
git commit -F /tmp/vouch-commit-msg.txt
```

---

## Task 5: Expose `kb.summarize_session` + CLI + starter config

**Files:**
- Modify: `src/vouch/capabilities.py` (add method to `METHODS`)
- Modify: `src/vouch/jsonl_server.py` (`_h_summarize_session` + `HANDLERS`)
- Modify: `src/vouch/server.py` (`kb_summarize_session` MCP tool)
- Modify: `src/vouch/cli.py` (`--split/--no-split` on finalize; new `capture summarize`)
- Modify: `src/vouch/storage.py` (`_starter_config` split block)
- Modify: `tests/test_session_split.py` (surface tests)

**Interfaces:**
- Produces: method `kb.summarize_session(session_id: str, mode: str = "auto") -> dict`. Registered in all four sites so `tests/test_capabilities.py::test_capabilities_matches_jsonl_handlers` passes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_split.py`:

```python
def test_kb_summarize_session_in_capabilities_and_handlers() -> None:
    from vouch import capabilities
    from vouch.jsonl_server import HANDLERS
    assert "kb.summarize_session" in capabilities.METHODS
    assert "kb.summarize_session" in HANDLERS


def test_jsonl_handler_summarizes(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    import vouch.jsonl_server as js
    _observe(store, "s1", 5)
    monkeypatch.setattr(js, "_store", lambda: store)
    res = js.HANDLERS["kb.summarize_session"]({"session_id": "s1"})
    assert res["mode"] == "mechanical"
    assert res["summary_proposal_id"] is not None


def test_starter_config_has_split_defaults() -> None:
    from vouch.storage import _starter_config
    split = _starter_config()["capture"]["split"]
    assert split["threshold_observations"] == 40
    assert split["enabled"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_session_split.py -k "capabilities_and_handlers or jsonl_handler or starter_config_has_split" -q`
Expected: FAIL — method not registered, no split block in starter config.

- [ ] **Step 3: Register the method in `capabilities.py`**

In `src/vouch/capabilities.py`, add one line to the `METHODS` list, right after `"kb.crystallize",`:

```python
    "kb.summarize_session",
```

- [ ] **Step 4: Add the JSONL handler**

In `src/vouch/jsonl_server.py`, add the handler next to `_h_compile` (both are LLM ingest ops):

```python
def _h_summarize_session(p: dict) -> dict:
    from . import session_split
    return session_split.summarize(
        _store(), p["session_id"], mode=p.get("mode", "auto"),
    )
```

And add to the `HANDLERS` dict, next to `"kb.compile": _h_compile,`:

```python
    "kb.summarize_session": _h_summarize_session,
```

- [ ] **Step 5: Add the MCP tool**

In `src/vouch/server.py`, add next to `kb_compile`:

```python
@mcp.tool()
def kb_summarize_session(
    session_id: str,
    mode: str = "auto",
) -> dict[str, Any]:
    """Summarize a captured session into PENDING page proposals.

    Reads the host-neutral observation buffer for `session_id` and files either
    one mechanical rollup page (small sessions) or several LLM-drafted topical
    `session` pages (large sessions). `mode` is "auto" | "split" | "mechanical".
    Long-running when it splits (the LLM call is synchronous). Never approves.
    """
    from . import session_split
    return session_split.summarize(_store(), session_id, mode=mode)
```

- [ ] **Step 6: Add CLI `--split/--no-split` + `capture summarize`**

In `src/vouch/cli.py`, add `from . import session_split` near the `capture as capture_mod` import. Then modify `capture_finalize_cmd` to accept the tri-state flag and forward a mode:

```python
@capture.command("finalize")
@click.option("--session-id", default=None, help="Session id (else read from stdin payload).")
@click.option("--split/--no-split", "force", default=None,
              help="Force LLM topical split or a single mechanical page (default: size-gated).")
def capture_finalize_cmd(session_id: str | None, force: bool | None) -> None:
    """Roll a session buffer into PENDING summary proposal(s) (SessionEnd hook payload on stdin)."""
    payload: dict[str, Any] = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    payload = loaded
            except json.JSONDecodeError:
                payload = {}
    sid = session_id or str(payload.get("session_id") or "")
    if not sid:
        return
    store = _capture_store()
    if store is None:
        return
    cwd = Path(str(payload.get("cwd") or ".")).resolve()
    transcript_raw = payload.get("transcript_path")
    transcript = Path(str(transcript_raw)) if transcript_raw else None
    mode = "auto" if force is None else ("split" if force else "mechanical")
    result = capture_mod.finalize(
        store, sid, cwd=cwd, project=cwd.name,
        generated_at=datetime.now(UTC).isoformat(),
        transcript_path=transcript, mode=mode,
    )
    _emit_json(result)
```

And add a new command right after it:

```python
@capture.command("summarize")
@click.argument("session_id")
@click.option("--split/--no-split", "force", default=None,
              help="Force split or mechanical (default: size-gated auto).")
def capture_summarize_cmd(session_id: str, force: bool | None) -> None:
    """Summarize a captured session into PENDING page proposals (size-gated)."""
    store = _capture_store()
    if store is None:
        _emit_json({"error": "no KB found"})
        return
    mode = "auto" if force is None else ("split" if force else "mechanical")
    result = session_split.summarize(
        store, session_id, mode=mode, generated_at=datetime.now(UTC).isoformat(),
    )
    _emit_json(result)
```

- [ ] **Step 7: Add the split block to `_starter_config`**

In `src/vouch/storage.py`, replace the `"capture"` block in `_starter_config` with:

```python
        "capture": {
            # auto-capture agent sessions into pending summaries.
            "enabled": True,
            "min_observations": 3,
            "split": {
                # llm topical split for large sessions; llm_cmd falls back to
                # compile.llm_cmd when null. see session_split.py.
                "enabled": True,
                "llm_cmd": None,
                "threshold_observations": 40,
                "max_pages": 6,
                "timeout_seconds": 180,
                "max_input_chars": 60000,
            },
        },
```

- [ ] **Step 8: Run surface tests + full suite**

Run: `.venv/bin/python -m pytest tests/test_session_split.py tests/test_capabilities.py -q`
Expected: PASS, including `test_capabilities_matches_jsonl_handlers`.

- [ ] **Step 9: Full CI gate**

Run: `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings && .venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: all green.

- [ ] **Step 10: Commit**

```bash
printf '%s\n' 'feat(session-split): expose kb.summarize_session across surfaces' > /tmp/vouch-commit-msg.txt
git add src/vouch/capabilities.py src/vouch/jsonl_server.py src/vouch/server.py src/vouch/cli.py src/vouch/storage.py tests/test_session_split.py
git commit -F /tmp/vouch-commit-msg.txt
```

---

## Self-Review

**Spec coverage:**
- host-neutral IR / reads only the buffer → Task 3 (`summarize` reads `capture._read_observations`), Task 4 (opaque `tool` labels) ✓
- `session_split.py` host-blind core → Tasks 3, 4 ✓
- `llm_draft.py` extracted from compile → Task 1 ✓
- three-tier size gate + mechanical default + fallback → Tasks 3, 4 ✓
- split prompt/contract, no `[claim: id]`, `type` forced to session → Task 4 (`build_split_prompt`, `_file_drafts`) ✓
- per-draft validation (title/body/dedupe/cap) → Task 4 (`_file_drafts`) ✓
- huge-input guard with honest `truncated` flag → Task 4 (`build_split_prompt`) ✓
- error handling → mechanical fallback, never raises to a hook → Task 4 (`summarize` try/except) ✓
- audit `session.split` event → Task 4 (`_audit_split`) ✓
- `kb.summarize_session` four sites + parity → Task 5 ✓
- CLI `--split/--no-split` + `capture summarize` → Task 5 ✓
- config block → Task 5 (`_starter_config`) + Task 2 (`load_split_config`) ✓
- intent priority (`Session.task` → header → host parser → filename): partially — `finalize` supplies the CC transcript parser as `intent`; `Session.task` wiring is left to the caller passing `intent`. NOTE: v1 resolves intent at the `finalize`/CLI layer; the buffer-`intent`-header source is not yet written by any adapter, so only the transcript-parser and filename-fallback tiers are exercised. This matches the spec's "pluggable" framing and is not a gap for v1.

**Placeholder scan:** No TBD/TODO; every code step shows complete code. The `# Task 4 inserts…` marker in Task 3 is replaced with real code in Task 4 Step 3. ✓

**Type consistency:** `summarize(...)` signature identical across Tasks 3–5; return dict keys (`captured`, `summary_proposal_id`, `summary_proposal_ids`, `mode`, `skipped`, `dropped`, `truncated`) consistent; `SplitConfig` fields match `load_split_config` and `_propose_split` usage; `capture.CAPTURE_PAGE_TYPE`/`CAPTURE_ACTOR` referenced consistently; `llm_draft.run_llm`/`parse_drafts` signatures match both compile wrappers and `_propose_split`. ✓
