# Session Auto-Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Once vouch is installed in a workspace, a Claude Code session that starts and ends is captured automatically and filed as one `PENDING` session-summary page proposal for human approval.

**Architecture:** Claude Code hooks harvest tool-use into an ephemeral, gitignored scratch buffer (`.vouch/captures/<session>.jsonl`) during the session; a `SessionEnd` hook rolls the buffer plus a `git diff` backstop into a single markdown summary and files it via the existing `proposals.propose_page` gate. Mechanical rollup, no LLM, no network. Capture never calls `approve()` — the review gate stays intact.

**Tech Stack:** Python 3, click (CLI), pydantic models, pytest. New module `src/vouch/capture.py`; CLI-only wiring (no new `kb.*` MCP/JSONL method).

## Global Constraints

- The CI gate must stay green: `pytest tests/ -q --ignore=tests/embeddings`, `mypy src`, `ruff check src tests` (i.e. `make check`). Type-annotate all new code.
- Conventional commits, lowercase, ≤72-char summary. **No `Co-Authored-By` trailer.**
- Stage files by name (`git add <path>`), never `git add -A`.
- `storage.py` stays pure I/O — all capture business logic lives in `capture.py`.
- Config is read defensively (yaml `safe_load` in try/except, `isinstance` per level, explicit coercion, hardcoded defaults) — never via pydantic at load time. Template: `volunteer_context.load_config`.
- This feature adds **no `kb.*` method** — no `@mcp.tool()`, no jsonl handler, no `capabilities.METHODS` entry. `test_capabilities` must remain untouched and green.
- No LLM and no network calls anywhere in the capture path.
- The `observe` path runs on every tool call; it must never crash the user's tool call (swallow all errors, always exit 0) and must stay minimal.
- Tests mirror module names: new tests go in `tests/test_capture.py`.
- Captured summaries are marked `proposed_by="vouch-capture"`, `page_type="session"`, `rationale="auto-captured session summary"` so reviewers can filter them.

**Reference signatures (already in the codebase — consume, do not redefine):**
- `KBStore.init(root: Path) -> KBStore`; `store.kb_dir: Path`; `store.config_path: Path` (= `kb_dir/config.yaml`).
- `discover_root(start: Path | None = None) -> Path` and `KBNotFoundError` in `vouch.storage`.
- `propose_page(store, *, title: str, body: str, page_type="concept", claim_ids=None, entity_ids=None, source_ids=None, proposed_by: str, tags=None, metadata=None, rationale=None, slug_hint=None, session_id=None, dry_run=False) -> Proposal`. Returns a `Proposal` with `.id`, `.proposed_by`, `.session_id`, `.kind == ProposalKind.PAGE`, `.status == ProposalStatus.PENDING`, and `.payload` = `{"id","title","body","type","claims","entities","sources","tags","metadata"}` (note `page_type` is stored under `payload["type"]`).
- `store.list_proposals(status: ProposalStatus | None = None) -> list[Proposal]`.
- `ProposalKind.PAGE`, `ProposalStatus.PENDING` in `vouch.models`.
- The built-in `"session"` page kind requires no citations and no frontmatter (`BUILTIN_PAGE_KINDS` default spec), so `propose_page(page_type="session")` with no citations validates.

---

### Task 1: capture config, buffer paths, starter-config + gitignore

**Files:**
- Create: `src/vouch/capture.py`
- Modify: `src/vouch/storage.py` (`_starter_config` ~line 75; `.gitignore` writer in `KBStore.init` ~line 224)
- Test: `tests/test_capture.py`

**Interfaces:**
- Produces: `CaptureConfig(enabled: bool, min_observations: int, dedup_window_seconds: float)`; `load_config(store: KBStore) -> CaptureConfig`; `captures_dir(store) -> Path`; `buffer_path(store, session_id: str) -> Path`. Module constants `CAPTURE_ACTOR = "vouch-capture"`, `CAPTURE_PAGE_TYPE = "session"`, `DEFAULT_ENABLED = True`, `DEFAULT_MIN_OBSERVATIONS = 3`, `DEFAULT_DEDUP_WINDOW_SECONDS = 60.0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capture.py
"""Auto-capture: config, buffer, observe, finalize."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import capture as cap
from vouch.storage import KBStore, _starter_config


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_load_config_defaults(store: KBStore) -> None:
    cfg = cap.load_config(store)
    assert cfg.enabled is True
    assert cfg.min_observations == 3
    assert cfg.dedup_window_seconds == 60.0


def test_load_config_reads_override(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  enabled: false\n  min_observations: 5\n"
    )
    cfg = cap.load_config(store)
    assert cfg.enabled is False
    assert cfg.min_observations == 5


def test_buffer_path_under_captures_dir(store: KBStore) -> None:
    p = cap.buffer_path(store, "sess-123")
    assert p == store.kb_dir / "captures" / "sess-123.jsonl"


def test_starter_config_has_capture_namespace() -> None:
    assert _starter_config()["capture"]["enabled"] is True


def test_init_gitignores_captures(tmp_path: Path) -> None:
    kb = KBStore.init(tmp_path)
    assert "captures/" in (kb.kb_dir / ".gitignore").read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vouch.capture'` (and `_starter_config` has no `capture` key).

- [ ] **Step 3: Create `src/vouch/capture.py` with config + paths**

```python
"""Auto-capture Claude Code sessions into review-gated summaries.

Passive harvest -> mechanical rollup -> one PENDING page proposal. No LLM.
`observe` appends compact observations to an ephemeral, gitignored scratch
buffer (`.vouch/captures/<session>.jsonl`); `finalize` rolls the buffer plus a
git-diff backstop into a single session-summary page proposal that a human
approves like any other write. Never calls approve() — the review gate stays
intact. See docs/superpowers/specs/2026-07-01-vouch-session-autocapture-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .storage import KBStore

DEFAULT_ENABLED = True
DEFAULT_MIN_OBSERVATIONS = 3
DEFAULT_DEDUP_WINDOW_SECONDS = 60.0
CAPTURE_ACTOR = "vouch-capture"
CAPTURE_PAGE_TYPE = "session"


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool = DEFAULT_ENABLED
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS


def load_config(store: KBStore) -> CaptureConfig:
    """Read ``capture:`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text())
    except (OSError, yaml.YAMLError):
        return CaptureConfig()
    if not isinstance(loaded, dict):
        return CaptureConfig()
    raw = loaded.get("capture")
    if not isinstance(raw, dict):
        return CaptureConfig()
    return CaptureConfig(
        enabled=bool(raw.get("enabled", DEFAULT_ENABLED)),
        min_observations=int(raw.get("min_observations", DEFAULT_MIN_OBSERVATIONS)),
        dedup_window_seconds=float(
            raw.get("dedup_window_seconds", DEFAULT_DEDUP_WINDOW_SECONDS)
        ),
    )


def captures_dir(store: KBStore) -> Path:
    return store.kb_dir / "captures"


def buffer_path(store: KBStore, session_id: str) -> Path:
    safe = session_id.replace("/", "_").replace("..", "_").strip() or "unknown"
    return captures_dir(store) / f"{safe}.jsonl"
```

- [ ] **Step 4: Add the `capture` namespace to `_starter_config` in `src/vouch/storage.py`**

Insert into the dict returned by `_starter_config()` (after the `"review"` block):

```python
        "capture": {
            # auto-capture claude code sessions into pending summaries.
            "enabled": True,
            "min_observations": 3,
        },
```

- [ ] **Step 5: Gitignore the captures buffer in `KBStore.init`**

In `src/vouch/storage.py`, change the `.gitignore` writer:

```python
        gi = kb.kb_dir / ".gitignore"
        if not gi.exists():
            # state.db is derived; proposed/ and captures/ are scratch space.
            gi.write_text("proposed/\ncaptures/\nstate.db\nstate.db-*\n")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q`
Expected: PASS (5 passed).

- [ ] **Step 7: Commit**

```bash
git add src/vouch/capture.py src/vouch/storage.py tests/test_capture.py
git commit -m "feat(capture): add capture config, buffer paths, gitignore"
```

---

### Task 2: observe — append an observation with dedup + tool summarizer

**Files:**
- Modify: `src/vouch/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `CaptureConfig`, `buffer_path`, `load_config` (Task 1).
- Produces:
  - `observe(store, session_id, *, tool: str, summary: str, files: list[str] | None = None, cmd: str | None = None, now: float | None = None, config: CaptureConfig | None = None) -> bool` — returns True if a line was written.
  - `summarize_tool(tool_name: str | None, tool_input: dict | None, tool_response: object) -> dict | None` — returns `{"tool","summary","files"(opt),"cmd"(opt)}` for observed tools, else None.
  - `_read_observations(path: Path) -> list[dict]` (internal, reused by Task 3).

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_capture.py
def test_observe_appends_line(store: KBStore) -> None:
    wrote = cap.observe(store, "s1", tool="Edit", summary="Edited a.py", now=100.0)
    assert wrote is True
    lines = cap.buffer_path(store, "s1").read_text().splitlines()
    assert len(lines) == 1
    assert "Edited a.py" in lines[0]


def test_observe_dedups_within_window(store: KBStore) -> None:
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=100.0)
    # identical within 60s window -> skipped
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=130.0) is False
    # same key past the window -> written again
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=200.0)
    assert len(cap.buffer_path(store, "s1").read_text().splitlines()) == 2


def test_observe_noop_when_disabled(store: KBStore) -> None:
    store.config_path.write_text("capture:\n  enabled: false\n")
    assert cap.observe(store, "s1", tool="Edit", summary="x") is False
    assert not cap.buffer_path(store, "s1").exists()


def test_summarize_tool_skips_unobserved() -> None:
    assert cap.summarize_tool("mcp__vouch__kb_search", {}, "") is None


def test_summarize_tool_edit() -> None:
    obs = cap.summarize_tool("Edit", {"file_path": "/repo/src/a.py"}, "ok")
    assert obs is not None
    assert obs["tool"] == "Edit"
    assert obs["files"] == ["/repo/src/a.py"]
    assert "a.py" in obs["summary"]


def test_summarize_tool_bash_flags_error() -> None:
    obs = cap.summarize_tool("Bash", {"command": "pytest"}, "1 failed, error")
    assert obs is not None
    assert obs["cmd"] == "pytest"
    assert "failed" in obs["summary"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q -k "observe or summarize"`
Expected: FAIL — `AttributeError: module 'vouch.capture' has no attribute 'observe'`.

- [ ] **Step 3: Implement observe + summarizer in `src/vouch/capture.py`**

Add imports at the top (`json`, `time`, `typing.Any`):

```python
import json
import time
from typing import Any
```

Append:

```python
_OBSERVED_TOOLS = frozenset({
    "Read", "Edit", "Write", "Update", "Bash",
    "Grep", "Glob", "WebFetch", "WebSearch", "Task", "NotebookEdit",
})


def _read_observations(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _dedup_key(tool: str, summary: str) -> str:
    return f"{tool}\x00{summary}"


def observe(
    store: KBStore,
    session_id: str,
    *,
    tool: str,
    summary: str,
    files: list[str] | None = None,
    cmd: str | None = None,
    now: float | None = None,
    config: CaptureConfig | None = None,
) -> bool:
    """Append one observation to the session buffer. Returns True if written."""
    cfg = config or load_config(store)
    if not cfg.enabled:
        return False
    ts = time.time() if now is None else now
    path = buffer_path(store, session_id)
    key = _dedup_key(tool, summary)
    for obs in reversed(_read_observations(path)):
        if ts - float(obs.get("ts", 0.0)) > cfg.dedup_window_seconds:
            break
        if _dedup_key(str(obs.get("tool", "")), str(obs.get("summary", ""))) == key:
            return False
    record: dict[str, Any] = {"ts": ts, "tool": tool, "summary": summary}
    if files:
        record["files"] = files
    if cmd:
        record["cmd"] = cmd
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return True


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] or path


def summarize_tool(
    tool_name: str | None,
    tool_input: dict[str, Any] | None,
    tool_response: object,
) -> dict[str, Any] | None:
    """Turn a PostToolUse payload into a compact observation, or None to skip."""
    if not tool_name or tool_name not in _OBSERVED_TOOLS:
        return None
    ti = tool_input or {}
    out: dict[str, Any] = {"tool": tool_name}
    fp = ti.get("file_path")
    if isinstance(fp, str) and fp:
        out["files"] = [fp]
    if tool_name in {"Read", "Edit", "Write", "Update", "NotebookEdit"}:
        name = _basename(fp) if isinstance(fp, str) and fp else "file"
        verb = {"Read": "Read", "Write": "Created"}.get(tool_name, "Edited")
        out["summary"] = f"{verb} {name}"
    elif tool_name == "Bash":
        cmd = ti.get("command")
        short = str(cmd).splitlines()[0][:60] if cmd else "command"
        out["cmd"] = str(cmd)[:200] if cmd else None
        text = str(tool_response).lower()
        failed = "error" in text or "failed" in text
        out["summary"] = f"Command failed: {short}" if failed else f"Ran: {short}"
    elif tool_name in {"Grep", "Glob"}:
        out["summary"] = f"{tool_name} {str(ti.get('pattern', ''))[:40]}"
    elif tool_name in {"WebFetch", "WebSearch"}:
        target = ti.get("url") or ti.get("query") or ""
        out["summary"] = f"Fetched: {str(target)[:60]}"
    else:  # Task
        out["summary"] = f"{tool_name} completed"
    if out.get("cmd") is None:
        out.pop("cmd", None)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/capture.py tests/test_capture.py
git commit -m "feat(capture): harvest tool-use observations with dedup"
```

---

### Task 3: finalize — mechanical rollup, git backstop, PENDING proposal

**Files:**
- Modify: `src/vouch/capture.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `observe`, `_read_observations`, `buffer_path`, `load_config`, `CAPTURE_ACTOR`, `CAPTURE_PAGE_TYPE` (Tasks 1-2); `propose_page` and models from the codebase.
- Produces:
  - `build_summary_body(session_id, observations, changed_files, git_stat, *, project=None, generated_at=None) -> tuple[str, str]` (title, markdown body) — pure.
  - `finalize(store, session_id, *, cwd: Path | None = None, project: str | None = None, generated_at: str | None = None, config: CaptureConfig | None = None) -> dict[str, Any]` — files at most one proposal, deletes the buffer, returns `{"captured": int, "summary_proposal_id": str | None, "skipped"?: str}`.
  - `pending_count(store) -> int` — number of PENDING proposals authored by `vouch-capture`.
  - `_git_changes(cwd: Path) -> tuple[list[str], str]` (internal; returns `([], "")` on any failure / non-repo).

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_capture.py
from vouch.models import ProposalKind, ProposalStatus


def _seed(store: KBStore, sid: str, n: int) -> None:
    for i in range(n):
        cap.observe(store, sid, tool="Edit", summary=f"Edited f{i}.py", now=float(i))


def test_finalize_files_one_pending_page(store: KBStore, tmp_path: Path) -> None:
    _seed(store, "s1", 3)
    result = cap.finalize(store, "s1", cwd=tmp_path)
    pid = result["summary_proposal_id"]
    assert pid is not None
    pend = store.list_proposals(ProposalStatus.PENDING)
    match = [p for p in pend if p.id == pid]
    assert len(match) == 1
    pr = match[0]
    assert pr.kind == ProposalKind.PAGE
    assert pr.proposed_by == cap.CAPTURE_ACTOR
    assert pr.payload["type"] == cap.CAPTURE_PAGE_TYPE
    assert pr.status == ProposalStatus.PENDING


def test_finalize_below_min_files_nothing(store: KBStore, tmp_path: Path) -> None:
    _seed(store, "s1", 2)  # below default min_observations=3, non-git cwd
    result = cap.finalize(store, "s1", cwd=tmp_path)
    assert result["summary_proposal_id"] is None
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_finalize_deletes_buffer(store: KBStore, tmp_path: Path) -> None:
    _seed(store, "s1", 3)
    cap.finalize(store, "s1", cwd=tmp_path)
    assert not cap.buffer_path(store, "s1").exists()


def test_finalize_noop_when_disabled(store: KBStore, tmp_path: Path) -> None:
    _seed(store, "s1", 5)
    store.config_path.write_text("capture:\n  enabled: false\n")
    result = cap.finalize(store, "s1", cwd=tmp_path)
    assert result["summary_proposal_id"] is None
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_build_summary_body_has_sections() -> None:
    obs = [
        {"ts": 1.0, "tool": "Edit", "summary": "Edited a.py", "files": ["a.py"]},
        {"ts": 2.0, "tool": "Bash", "summary": "Ran: pytest", "cmd": "pytest"},
    ]
    title, body = cap.build_summary_body("s1", obs, ["a.py"], "a.py | 2 +-")
    assert "s1" in title
    assert "files modified this session" in body.lower()
    assert "## activity" in body.lower()
    assert "a.py" in body


def test_pending_count_counts_capture_actor(store: KBStore, tmp_path: Path) -> None:
    _seed(store, "s1", 3)
    cap.finalize(store, "s1", cwd=tmp_path)
    assert cap.pending_count(store) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q -k "finalize or summary_body or pending_count"`
Expected: FAIL — `AttributeError: module 'vouch.capture' has no attribute 'finalize'`.

- [ ] **Step 3: Implement finalize + rollup + git backstop in `src/vouch/capture.py`**

Add imports at the top:

```python
import subprocess

from .models import ProposalStatus
from .proposals import propose_page
```

Append:

```python
def _git_changes(cwd: Path) -> tuple[list[str], str]:
    """Return (changed_files, diff_stat). Empty on any failure / non-repo."""
    try:
        names = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [], ""
    files = [f for f in names.stdout.splitlines() if f.strip()]
    if not files:
        return [], ""
    try:
        stat = subprocess.run(
            ["git", "diff", "HEAD", "--stat"],
            cwd=cwd, capture_output=True, text=True, timeout=3, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        stat = ""
    return files, stat


def build_summary_body(
    session_id: str,
    observations: list[dict[str, Any]],
    changed_files: list[str],
    git_stat: str,
    *,
    project: str | None = None,
    generated_at: str | None = None,
) -> tuple[str, str]:
    tool_counts: dict[str, int] = {}
    files: set[str] = set(changed_files)
    commands: list[str] = []
    for obs in observations:
        tool = str(obs.get("tool", ""))
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        for f in obs.get("files") or []:
            files.add(str(f))
        cmd = obs.get("cmd")
        if cmd:
            commands.append(str(cmd))
    title = f"session summary: {project or 'workspace'} ({session_id})"
    lines: list[str] = [f"# {title}", ""]
    if generated_at:
        lines.append(f"- generated: {generated_at}")
    lines += [f"- session: `{session_id}`", f"- observations: {len(observations)}", ""]
    if files:
        lines += ["## files modified this session", ""]
        lines += [f"- {f}" for f in sorted(files)[:20]]
        lines.append("")
    if git_stat:
        lines += ["## git changes", "", "```", git_stat, "```", ""]
    if tool_counts:
        lines += ["## activity", ""]
        lines += [f"- {t}: {tool_counts[t]}" for t in sorted(tool_counts)]
        lines.append("")
    if commands:
        lines += ["## notable commands", ""]
        lines += [f"- `{c}`" for c in commands[:10]]
        lines.append("")
    if observations:
        lines += ["## observations", ""]
        lines += [f"- {o.get('summary', '')}" for o in observations[:30]]
        lines.append("")
    return title, "\n".join(lines).rstrip() + "\n"


def finalize(
    store: KBStore,
    session_id: str,
    *,
    cwd: Path | None = None,
    project: str | None = None,
    generated_at: str | None = None,
    config: CaptureConfig | None = None,
) -> dict[str, Any]:
    """Roll a session buffer into one PENDING summary proposal. No approve()."""
    cfg = config or load_config(store)
    path = buffer_path(store, session_id)
    observations = _read_observations(path)
    if not cfg.enabled:
        return {"captured": len(observations), "summary_proposal_id": None,
                "skipped": "disabled"}
    changed_files, git_stat = _git_changes(cwd or Path.cwd())
    total = len(observations) + len(changed_files)
    if total < cfg.min_observations:
        if path.exists():
            path.unlink()
        return {"captured": total, "summary_proposal_id": None,
                "skipped": "below-min"}
    title, body = build_summary_body(
        session_id, observations, changed_files, git_stat,
        project=project, generated_at=generated_at,
    )
    proposal = propose_page(
        store,
        title=title,
        body=body,
        page_type=CAPTURE_PAGE_TYPE,
        proposed_by=CAPTURE_ACTOR,
        session_id=session_id,
        rationale="auto-captured session summary",
    )
    if path.exists():
        path.unlink()
    return {"captured": total, "summary_proposal_id": proposal.id}


def pending_count(store: KBStore) -> int:
    return sum(
        1 for p in store.list_proposals(ProposalStatus.PENDING)
        if p.proposed_by == CAPTURE_ACTOR
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q`
Expected: PASS (all capture tests).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/capture.py tests/test_capture.py
git commit -m "feat(capture): roll a session into one pending summary proposal"
```

---

### Task 4: CLI `vouch capture` group (observe, finalize, banner)

**Files:**
- Modify: `src/vouch/cli.py`
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `capture.observe`, `capture.finalize`, `capture.summarize_tool`, `capture.pending_count`, `capture.CAPTURE_ACTOR` (Tasks 2-3); `KBStore`, `discover_root`, `KBNotFoundError`, `_emit_json` (existing in cli.py).
- Produces: CLI commands `vouch capture observe`, `vouch capture finalize [--session-id ID]`, `vouch capture banner`. These are **CLI-only** — no MCP/JSONL/capabilities changes.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_capture.py
import json as _json

from click.testing import CliRunner

from vouch.cli import cli
from vouch.models import ProposalStatus


def _run(store: KBStore, args: list[str], stdin: str = "") -> object:
    runner = CliRunner()
    return runner.invoke(
        cli, args, input=stdin,
        env={"VOUCH_KB_PATH": str(store.kb_dir)},
    )


def test_cli_observe_appends(store: KBStore) -> None:
    payload = _json.dumps({
        "session_id": "cc-1",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/r/a.py"},
        "tool_response": "ok",
    })
    res = _run(store, ["capture", "observe"], stdin=payload)
    assert res.exit_code == 0
    assert cap.buffer_path(store, "cc-1").exists()


def test_cli_observe_never_errors_on_garbage(store: KBStore) -> None:
    res = _run(store, ["capture", "observe"], stdin="not json")
    assert res.exit_code == 0


def test_cli_finalize_files_proposal(store: KBStore) -> None:
    for i in range(3):
        cap.observe(store, "cc-2", tool="Edit", summary=f"Edited f{i}.py", now=float(i))
    payload = _json.dumps({"session_id": "cc-2", "cwd": str(store.kb_dir.parent)})
    res = _run(store, ["capture", "finalize"], stdin=payload)
    assert res.exit_code == 0
    pend = store.list_proposals(ProposalStatus.PENDING)
    assert any(p.proposed_by == cap.CAPTURE_ACTOR for p in pend)


def test_cli_banner_emits_when_pending(store: KBStore) -> None:
    for i in range(3):
        cap.observe(store, "cc-3", tool="Edit", summary=f"Edited f{i}.py", now=float(i))
    cap.finalize(store, "cc-3", cwd=store.kb_dir.parent)
    res = _run(store, ["capture", "banner"])
    assert res.exit_code == 0
    assert "awaiting review" in res.output


def test_cli_banner_silent_when_none(store: KBStore) -> None:
    res = _run(store, ["capture", "banner"])
    assert res.exit_code == 0
    assert res.output.strip() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q -k cli`
Expected: FAIL — `No such command 'capture'`.

- [ ] **Step 3: Add the CLI group to `src/vouch/cli.py`**

Confirm these imports exist near the top of `cli.py` (add any missing): `import sys`, `import json`, `from pathlib import Path`, `from datetime import UTC, datetime`, `from vouch import capture as capture_mod`, and `from vouch.storage import KBStore, discover_root, KBNotFoundError`. Append the group near the other `@cli.group()` blocks (e.g. after the `session` group):

```python
@cli.group()
def capture() -> None:
    """Automatic session capture (driven by claude code hooks)."""


def _capture_store() -> KBStore | None:
    """Locate the KB without the sys.exit(2) that _load_store does — hooks
    must never abort the host."""
    try:
        return KBStore(discover_root())
    except KBNotFoundError:
        return None


@capture.command("observe")
def capture_observe_cmd() -> None:
    """Append one observation from a PostToolUse hook payload (stdin JSON)."""
    if sys.stdin.isatty():
        return
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return
        obs = capture_mod.summarize_tool(
            payload.get("tool_name"),
            payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {},
            payload.get("tool_response"),
        )
        if obs is None:
            return
        store = _capture_store()
        if store is None:
            return
        capture_mod.observe(
            store, session_id,
            tool=obs["tool"], summary=obs["summary"],
            files=obs.get("files"), cmd=obs.get("cmd"),
        )
    except Exception:  # noqa: BLE001 - a capture failure must never break a tool call
        return


@capture.command("finalize")
@click.option("--session-id", default=None, help="Session id (else read from stdin payload).")
def capture_finalize_cmd(session_id: str | None) -> None:
    """Roll a session buffer into a PENDING summary (SessionEnd hook payload on stdin)."""
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
    result = capture_mod.finalize(
        store, sid, cwd=cwd, project=cwd.name,
        generated_at=datetime.now(UTC).isoformat(),
    )
    _emit_json(result)


@capture.command("banner")
def capture_banner_cmd() -> None:
    """Emit a SessionStart nudge if captured summaries await review."""
    store = _capture_store()
    if store is None:
        return
    n = capture_mod.pending_count(store)
    if n:
        click.echo(
            f"🔔 {n} auto-captured session summary(ies) awaiting review — "
            f"run `vouch review`."
        )
```

Note: `Any` is used in the finalize command — ensure `from typing import Any` is imported in `cli.py` (it already is in most vouch modules; add if `ruff`/`mypy` flags it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q`
Expected: PASS (all capture tests including CLI).

- [ ] **Step 5: Run the full gate**

Run: `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings && .venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: PASS. In particular `tests/test_capabilities.py` stays green (no new `kb.*` method was added).

- [ ] **Step 6: Commit**

```bash
git add src/vouch/cli.py tests/test_capture.py
git commit -m "feat(capture): add vouch capture observe/finalize/banner cli"
```

---

### Task 5: Claude Code adapter wiring + changelog

**Files:**
- Modify: `adapters/claude-code/.claude/settings.json`
- Modify: `adapters/claude-code/install.yaml` (T4 comment)
- Modify: `CHANGELOG.md` (`[Unreleased]`)
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: the `vouch capture observe|finalize|banner` commands (Task 4).
- Produces: adapter hooks that drive capture on install; a regression test asserting the hooks exist.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_capture.py
def test_adapter_settings_wires_capture_hooks() -> None:
    import json as J
    from pathlib import Path as P
    root = P(__file__).resolve().parents[1]
    settings = J.loads(
        (root / "adapters/claude-code/.claude/settings.json").read_text()
    )
    hooks = settings["hooks"]

    def commands(event: str) -> list[str]:
        out: list[str] = []
        for group in hooks.get(event, []):
            for h in group.get("hooks", []):
                out.append(h.get("command", ""))
        return out

    assert any("capture observe" in c for c in commands("PostToolUse"))
    assert any("capture finalize" in c for c in commands("SessionEnd"))
    assert any("capture banner" in c for c in commands("SessionStart"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q -k adapter`
Expected: FAIL — `KeyError: 'PostToolUse'` (or the assertions fail).

- [ ] **Step 3: Update `adapters/claude-code/.claude/settings.json`**

Replace the `"hooks"` object with (keep the existing `"permissions"` block unchanged):

```json
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "vouch status --json || true"
          },
          {
            "type": "command",
            "command": "vouch capture banner || true"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "vouch capture observe || true"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "vouch capture finalize || true"
          }
        ]
      }
    ]
  }
```

- [ ] **Step 4: Update the T4 comment in `adapters/claude-code/install.yaml`**

Change the T4 description line to reflect the expanded hook surface:

```
# T4 = `.claude/settings.json`: SessionStart (kb status + capture review banner),
#      PostToolUse (capture observe), SessionEnd (capture finalize), plus
#      read-only kb_* auto-allow.
```

- [ ] **Step 5: Add a CHANGELOG entry under `[Unreleased]`**

Add under the `### Added` list in `CHANGELOG.md` (create the `### Added` subsection if the `[Unreleased]` block lacks one):

```markdown
- auto-capture: claude code sessions are harvested via hooks and filed as a
  single pending session-summary proposal for human approval (`vouch capture`
  cli + adapter hooks; opt out with `capture.enabled: false`).
```

- [ ] **Step 6: Run the test + full gate**

Run: `.venv/bin/python -m pytest tests/test_capture.py -q -k adapter`
Expected: PASS.

Run: `make check`
Expected: PASS (all tests, mypy, ruff green).

- [ ] **Step 7: Commit**

```bash
git add adapters/claude-code/.claude/settings.json adapters/claude-code/install.yaml CHANGELOG.md tests/test_capture.py
git commit -m "feat(capture): wire claude code adapter hooks for session capture"
```

---

## Self-Review

**1. Spec coverage** — every spec section maps to a task:
- scratch buffer (spec §1) → Task 1 (`buffer_path`, gitignore) + Task 2 (append).
- `vouch capture observe` (spec §2) → Task 2 (`observe`, `summarize_tool`) + Task 4 (CLI).
- `vouch capture finalize` (spec §3) → Task 3 (`finalize`, `build_summary_body`, git backstop) + Task 4 (CLI).
- notification / next-session banner (spec §4) → Task 3 (`pending_count`) + Task 4 (`banner`) + Task 5 (SessionStart hook).
- config `capture.*` (spec §5) → Task 1 (`load_config`, `_starter_config`).
- adapter changes (spec §6) → Task 5.
- review-gate compliance → `finalize` only calls `propose_page` (PENDING); asserted in `test_finalize_files_one_pending_page`.
- registration/parity (no `kb.*`) → Task 4 Step 5 gate explicitly re-runs `test_capabilities`.
- out-of-scope items (no LLM/network, no per-observation proposals, no auto-approve, stale sweep deferred) → honored; nothing in any task adds them.

**2. Placeholder scan** — no TBD/TODO; every code step shows complete code; the one broad `except Exception` is intentional and commented (a capture failure must never break a tool call).

**3. Type consistency** — names used consistently across tasks: `observe`, `finalize`, `build_summary_body`, `summarize_tool`, `pending_count`, `buffer_path`, `CaptureConfig`, `CAPTURE_ACTOR`, `CAPTURE_PAGE_TYPE`. `propose_page` return consumed as `.id`, `.kind`, `.proposed_by`, `.status`, `.payload["type"]` — matches the reference signature. `_read_observations` defined in Task 2, reused in Task 3.

## Open items surfaced during planning (non-blocking)

- **`observe` startup cost** — importing `capture.py` pulls `proposals`/models on every tool call. Accepted for v1 per the spec's risk note; the fallback (a standalone appender script) is out of scope. If per-tool latency proves noticeable in practice, revisit before broadening beyond Claude Code.
- **stale sessions** — a hard crash skips `SessionEnd`, orphaning a buffer (harmless scratch). A `vouch capture finalize --stale` sweep is a deliberate follow-up, not in this plan.
