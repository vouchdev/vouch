# Session Transcript Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only Session Transcript Viewer to the vouch console that opens a captured Claude Code / Codex session and renders its full transcript (thinking, assistant text, tool calls with inputs + outputs, diffs, subagents), faithfully reproducing agentsview's rendering vocabulary.

**Architecture:** A new `kb.session_transcript` RPC locates the raw agent JSONL on disk on demand, parses it into a normalized block schema, and returns it (degrading to vouch's compact capture observations when the raw file is gone). A new React **Sessions** tab lists sessions (`kb.list_sessions`, fanned out over scoped projects) and renders the selected session's blocks. No sync engine, no database — parsing happens per-open.

**Tech Stack:** Backend — Python 3.11+, stdlib `json`/`pathlib`, `KBStore`, pytest. Frontend — React 19, TypeScript, Tailwind v4, `@tanstack/react-query`, `react-markdown`, `lucide-react`, vitest + Testing Library, Playwright.

## Global Constraints

- Repo: `vouch` at `/home/a/Dev/plind-junior/vouch`. Backend under `src/vouch/`, tests under `tests/`. Frontend under `webapp/`.
- Follow `vouch/AGENTS.md`, NOT agentsview's CLAUDE.md. Conventional commits `<type>(<scope>): <summary>` (types: feat|fix|refactor|test|docs|chore|perf|ci|style|build|revert), ≤72-char summary.
- **No `Co-Authored-By: <AI tool>` trailer** in commits. No secrets or absolute machine paths as PII in commit messages.
- Do NOT switch/create branches without explicit user permission. Current branch is `test`; there are pre-existing uncommitted changes that are NOT ours — `git add` only our own files, never `git add -A`.
- Backend gates before every commit that touches Python: `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings`, `.venv/bin/python -m mypy src`, `.venv/bin/python -m ruff check src tests`.
- Frontend gates before every commit that touches `webapp/`: `npm run test` and `npm run build` (from `webapp/`).
- Every new `kb.*` method MUST be added to both `capabilities.METHODS` and `jsonl_server.HANDLERS` or `test_capabilities_matches_jsonl_handlers` fails.
- Tests assert observable behavior, not implementation strings (testing-without-tautologies). Backend uses plain pytest `assert`.

## Normalized transcript schema (the contract both sides share)

Returned by `kb.session_transcript`. Tool results are paired into their tool_use block server-side.

```jsonc
// available === true
{
  "available": true,
  "source": { "agent": "claude", "path": "<absolute path>" },
  "session": {
    "id": "…", "agent": "claude",
    "cwd": "…"|null, "git_branch": "…"|null, "title": "…"|null,
    "started_at": "ISO"|null, "ended_at": "ISO"|null, "model": "…"|null,
    "tokens": { "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0 }
  },
  "messages": [
    { "role": "user"|"assistant", "id": "…"|null, "model": "…"|null,
      "timestamp": "ISO"|null,
      "tokens": { "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0 }|null,
      "blocks": [
        { "type": "text", "text": "…" },
        { "type": "thinking", "text": "…" },
        { "type": "tool_use", "id": "…", "name": "Bash", "input": {…},
          "result": { "content": "…", "is_error": false,
                      "subagent_session_id": "…"|null }|null }
      ] }
  ],
  "truncated": false
}
// available === false
{ "available": false, "reason": "…",
  "observations": [ { "ts": 0.0, "tool": "Edit", "summary": "Edited x.go",
                      "files": [...]|undefined, "cmd": "…"|undefined } ] }
```

---

## File Structure

Backend:
- Create `src/vouch/transcript.py` — locator + Claude parser + Codex parser + `load_transcript` orchestrator. One responsibility: turn a session id into the normalized schema (or a degraded result).
- Modify `src/vouch/jsonl_server.py` — add `_h_session_transcript` handler + register in `HANDLERS`.
- Modify `src/vouch/capabilities.py` — add `"kb.session_transcript"` to `METHODS`.
- Create `tests/test_session_transcript.py` — parser/locator/handler/degradation tests + fixtures inline.

Frontend (`webapp/`):
- Create `src/lib/transcript.ts` — TS types mirroring the schema + `fetchTranscript(conn, sessionId, agent?)`.
- Create `src/components/transcript/DiffView.tsx`, `CodeBlock.tsx`, `ThinkingBlock.tsx`, `ToolBlock.tsx`, `MessageBlock.tsx` — the block renderers.
- Create `src/views/TranscriptView.tsx` — fetches + renders one session (incl. degraded + subagent lazy-load).
- Create `src/views/SessionsView.tsx` — master–detail list + selection.
- Modify `src/App.tsx` — add `/sessions` route.
- Modify `src/components/Shell.tsx` — add Sessions nav item.
- Create colocated `*.test.tsx` for each component/view.
- Create `webapp/e2e/sessions.spec.ts` — smoke.

---

## PHASE 1 — Backend: Claude locator, parser, RPC

### Task 1: Claude file locator

**Files:**
- Create: `src/vouch/transcript.py`
- Test: `tests/test_session_transcript.py`

**Interfaces:**
- Produces: `find_claude_file(session_id: str) -> Path | None`; `_VALID_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")`; env override `VOUCH_CLAUDE_PROJECTS_DIR`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_transcript.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import transcript


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_find_claude_file_top_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    sid = "ad5d5e5f-0097-494c-8316-b01aed5dabf2"
    f = root / "-home-a-Dev-agentsview" / f"{sid}.jsonl"
    _write_jsonl(f, [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(root))
    assert transcript.find_claude_file(sid) == f


def test_find_claude_file_subagent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    parent = "11111111-1111-1111-1111-111111111111"
    child = "22222222-2222-2222-2222-222222222222"
    f = root / "-proj" / parent / "subagents" / "jobs" / f"{child}.jsonl"
    _write_jsonl(f, [{"type": "assistant", "message": {"role": "assistant", "content": []}}])
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(root))
    assert transcript.find_claude_file(child) == f


def test_find_claude_file_rejects_bad_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert transcript.find_claude_file("../etc/passwd") is None
    assert transcript.find_claude_file("*") is None
    assert transcript.find_claude_file("") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'vouch.transcript'` / `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/vouch/transcript.py
"""Locate and parse raw agent session transcripts on demand.

Given a captured session id, find the raw JSONL the agent wrote
(Claude Code under ~/.claude/projects, Codex rollouts under
$CODEX_HOME/sessions) and normalize it into a block schema the vouch
console renders. Read-only: never writes to the KB. When the raw file is
gone we degrade to vouch's compact capture observations instead.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Session ids are UUID-shaped; reject anything else so a hostile id can't
# widen a glob or traverse out of the projects tree.
_VALID_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _claude_projects_root() -> Path:
    env = os.environ.get("VOUCH_CLAUDE_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def find_claude_file(session_id: str) -> Path | None:
    """The raw Claude Code JSONL for ``session_id``, or None.

    Claude names each session file ``<id>.jsonl`` under a per-cwd project
    dir; subagent transcripts live under ``<parent>/subagents/**``. The
    file stem is the id, so a literal name match (no id interpolation into
    a glob) locates it.
    """
    if not _VALID_ID.match(session_id):
        return None
    root = _claude_projects_root()
    if not root.is_dir():
        return None
    name = f"{session_id}.jsonl"
    for project in root.iterdir():
        if not project.is_dir():
            continue
        top = project / name
        if top.is_file():
            return top
    for candidate in root.glob(f"*/*/subagents/**/{name}"):
        if candidate.is_file():
            return candidate
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/transcript.py tests/test_session_transcript.py
git commit -m "feat(transcript): locate raw Claude session files by id"
```

---

### Task 2: Claude transcript parser

**Files:**
- Modify: `src/vouch/transcript.py`
- Test: `tests/test_session_transcript.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `parse_claude_transcript(path: Path, *, max_messages: int = 2000) -> dict[str, Any]` returning `{"session": {...}, "messages": [...], "truncated": bool}` per the schema; helper `_norm_tokens(usage: dict) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_session_transcript.py

_CLAUDE_LINES = [
    {"type": "user", "cwd": "/repo", "gitBranch": "main",
     "timestamp": "2026-07-10T04:44:19.043Z",
     "message": {"role": "user", "content": [{"type": "text", "text": "fix the bug"}]}},
    {"type": "ai-title", "aiTitle": "Fix the bug"},
    {"type": "assistant", "timestamp": "2026-07-10T04:44:35.759Z",
     "message": {"id": "msg_1", "model": "claude-opus-4-8", "role": "assistant",
                 "content": [{"type": "thinking", "thinking": "let me look"}],
                 "usage": {"input_tokens": 100, "output_tokens": 10,
                           "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2}}},
    {"type": "assistant", "timestamp": "2026-07-10T04:44:36.771Z",
     "message": {"id": "msg_1", "model": "claude-opus-4-8", "role": "assistant",
                 "content": [{"type": "text", "text": "I'll edit it."}],
                 "usage": {"input_tokens": 100, "output_tokens": 10}}},
    {"type": "assistant", "timestamp": "2026-07-10T04:44:36.772Z",
     "message": {"id": "msg_1", "model": "claude-opus-4-8", "role": "assistant",
                 "content": [{"type": "tool_use", "id": "tu_1", "name": "Bash",
                              "input": {"command": "go test ./..."}}],
                 "usage": {"input_tokens": 100, "output_tokens": 10}}},
    {"type": "user",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok\n", "is_error": False}]}},
]


def test_parse_claude_pairs_result_and_merges_by_message_id(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _write_jsonl(f, _CLAUDE_LINES)
    out = transcript.parse_claude_transcript(f)

    assert out["session"]["cwd"] == "/repo"
    assert out["session"]["git_branch"] == "main"
    assert out["session"]["title"] == "Fix the bug"
    assert out["session"]["model"] == "claude-opus-4-8"
    assert out["truncated"] is False

    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant"]  # tool_result user entry is consumed, not a message

    # the three msg_1 assistant lines merged into one message, in order
    a = out["messages"][1]
    assert [b["type"] for b in a["blocks"]] == ["thinking", "text", "tool_use"]
    tu = a["blocks"][2]
    assert tu["name"] == "Bash" and tu["input"] == {"command": "go test ./..."}
    assert tu["result"] == {"content": "ok\n", "is_error": False, "subagent_session_id": None}
    assert a["tokens"] == {"input": 100, "output": 10, "cache_read": 5, "cache_creation": 2}


def test_parse_claude_truncates_at_cap(tmp_path: Path) -> None:
    lines = [{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": f"m{i}"}]}}
             for i in range(5)]
    f = tmp_path / "big.jsonl"
    _write_jsonl(f, lines)
    out = transcript.parse_claude_transcript(f, max_messages=3)
    assert out["truncated"] is True
    assert len(out["messages"]) == 3


def test_parse_claude_tolerates_malformed_lines(tmp_path: Path) -> None:
    f = tmp_path / "m.jsonl"
    f.write_text('{"bad json\n{"type":"user","message":{"role":"user","content":"hey"}}\n', encoding="utf-8")
    out = transcript.parse_claude_transcript(f)
    assert len(out["messages"]) == 1
    assert out["messages"][0]["blocks"] == [{"type": "text", "text": "hey"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -k parse_claude -q`
Expected: FAIL with `AttributeError: module 'vouch.transcript' has no attribute 'parse_claude_transcript'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/vouch/transcript.py

def _norm_tokens(usage: dict[str, Any]) -> dict[str, int]:
    def i(key: str) -> int:
        v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) else 0
    return {
        "input": i("input_tokens"),
        "output": i("output_tokens"),
        "cache_read": i("cache_read_input_tokens"),
        "cache_creation": i("cache_creation_input_tokens"),
    }


def _result_text(content: Any) -> str:
    """tool_result.content is a string, or a list of {type:text,text} parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(p.get("text", "")) for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
        if parts:
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def parse_claude_transcript(path: Path, *, max_messages: int = 2000) -> dict[str, Any]:
    """Parse a Claude Code JSONL into the normalized transcript schema.

    Single forward pass: assistant content blocks that share a
    ``message.id`` merge into one logical message; a later ``tool_result``
    (in a user entry) is paired into the matching ``tool_use`` block by id
    and its user entry is not emitted as a standalone message.
    """
    messages: list[dict[str, Any]] = []
    tool_by_id: dict[str, dict[str, Any]] = {}
    session: dict[str, Any] = {
        "id": path.stem, "agent": "claude", "cwd": None, "git_branch": None,
        "title": None, "started_at": None, "ended_at": None, "model": None,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    truncated = False
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current["blocks"]:
            messages.append(current)
        current = None

    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if session["cwd"] is None and isinstance(obj.get("cwd"), str):
                session["cwd"] = obj["cwd"]
            if session["git_branch"] is None and isinstance(obj.get("gitBranch"), str):
                session["git_branch"] = obj["gitBranch"]
            ts = obj.get("timestamp")
            if isinstance(ts, str):
                if session["started_at"] is None:
                    session["started_at"] = ts
                session["ended_at"] = ts
            t = obj.get("type")
            if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                session["title"] = obj["aiTitle"]
                continue
            if t not in ("user", "assistant"):
                continue
            if len(messages) >= max_messages:
                truncated = True
                break
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")

            if t == "assistant":
                mid = msg.get("id") if isinstance(msg.get("id"), str) else None
                if current is None or current.get("id") != mid:
                    flush()
                    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
                    model = msg.get("model") if isinstance(msg.get("model"), str) else None
                    if model and session["model"] is None:
                        session["model"] = model
                    current = {"role": "assistant", "id": mid, "model": model,
                               "timestamp": ts if isinstance(ts, str) else None,
                               "tokens": _norm_tokens(usage), "blocks": []}
                    tok = current["tokens"]
                    for k in session["tokens"]:
                        session["tokens"][k] += tok[k]
                parts = content if isinstance(content, list) else []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "thinking":
                        text = str(part.get("thinking", "")).strip()
                        if text:
                            current["blocks"].append({"type": "thinking", "text": text})
                    elif ptype == "text":
                        text = str(part.get("text", "")).strip()
                        if text:
                            current["blocks"].append({"type": "text", "text": text})
                    elif ptype == "tool_use":
                        tid = part.get("id")
                        block = {"type": "tool_use", "id": tid,
                                 "name": str(part.get("name", "")),
                                 "input": part.get("input") if isinstance(part.get("input"), dict) else {},
                                 "result": None}
                        current["blocks"].append(block)
                        if isinstance(tid, str):
                            tool_by_id[tid] = block
                continue

            # user entry
            flush()
            if isinstance(content, str):
                text = content.strip()
                if text:
                    messages.append({"role": "user", "id": None, "model": None,
                                     "timestamp": ts if isinstance(ts, str) else None,
                                     "tokens": None, "blocks": [{"type": "text", "text": text}]})
                continue
            parts = content if isinstance(content, list) else []
            user_blocks: list[dict[str, Any]] = []
            agent_id = None
            tur = obj.get("toolUseResult")
            if isinstance(tur, dict) and isinstance(tur.get("agentId"), str):
                agent_id = tur["agentId"]
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_result":
                    tid = part.get("tool_use_id")
                    block = tool_by_id.get(tid) if isinstance(tid, str) else None
                    if block is not None:
                        block["result"] = {
                            "content": _result_text(part.get("content")),
                            "is_error": bool(part.get("is_error", False)),
                            "subagent_session_id": agent_id,
                        }
                elif part.get("type") == "text":
                    text = str(part.get("text", "")).strip()
                    if text:
                        user_blocks.append({"type": "text", "text": text})
            if user_blocks:
                messages.append({"role": "user", "id": None, "model": None,
                                 "timestamp": ts if isinstance(ts, str) else None,
                                 "tokens": None, "blocks": user_blocks})
    flush()
    return {"session": session, "messages": messages, "truncated": truncated}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -k parse_claude -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run mypy + ruff**

Run: `.venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: no errors. (Fix any typing nits inline, e.g. annotate `parts: list[Any]`.)

- [ ] **Step 6: Commit**

```bash
git add src/vouch/transcript.py tests/test_session_transcript.py
git commit -m "feat(transcript): parse Claude JSONL into normalized blocks"
```

---

### Task 3: `load_transcript` orchestrator with degradation + size cap

**Files:**
- Modify: `src/vouch/transcript.py`
- Test: `tests/test_session_transcript.py`

**Interfaces:**
- Consumes: `find_claude_file`, `parse_claude_transcript`, `capture.buffer_path`, `capture._read_observations`.
- Produces: `load_transcript(store: KBStore, session_id: str, *, agent: str | None = None) -> dict[str, Any]` returning either the available schema (with `source`) or the degraded schema.
- Constants: `MAX_FILE_BYTES = 25 * 1024 * 1024`, `MAX_MESSAGES = 2000`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_session_transcript.py
from vouch import capture
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_load_transcript_available(tmp_path: Path, store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    sid = "ad5d5e5f-0097-494c-8316-b01aed5dabf2"
    f = root / "-repo" / f"{sid}.jsonl"
    _write_jsonl(f, _CLAUDE_LINES)
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(root))
    out = transcript.load_transcript(store, sid)
    assert out["available"] is True
    assert out["source"] == {"agent": "claude", "path": str(f)}
    assert out["session"]["title"] == "Fix the bug"


def test_load_transcript_degrades_to_observations(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(store.kb_dir / "none"))
    sid = "99999999-9999-9999-9999-999999999999"
    capture.observe(store, sid, tool="Edit", summary="Edited x.go")
    out = transcript.load_transcript(store, sid)
    assert out["available"] is False
    assert out["observations"][0]["tool"] == "Edit"
    assert "reason" in out


def test_load_transcript_degrades_when_oversized(tmp_path: Path, store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    sid = "88888888-8888-8888-8888-888888888888"
    f = root / "-repo" / f"{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x" * 32, encoding="utf-8")
    monkeypatch.setenv("VOUCH_CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.setattr(transcript, "MAX_FILE_BYTES", 16)
    out = transcript.load_transcript(store, sid)
    assert out["available"] is False
    assert "too large" in out["reason"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -k load_transcript -q`
Expected: FAIL (`load_transcript` undefined).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/vouch/transcript.py (add import at top: from .capture import _read_observations, buffer_path)
# and: from .storage import KBStore   (TYPE_CHECKING is fine, but a runtime import is used by callers)

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_MESSAGES = 2000


def _degraded(store: "KBStore", session_id: str, reason: str) -> dict[str, Any]:
    obs = _read_observations(buffer_path(store, session_id))
    return {"available": False, "reason": reason, "observations": obs}


def load_transcript(
    store: "KBStore", session_id: str, *, agent: str | None = None
) -> dict[str, Any]:
    """Locate + parse the raw transcript for ``session_id``.

    ``agent`` restricts the search ("claude" | "codex"); when None both are
    tried. Returns the normalized schema on success, or a degraded result
    (compact capture observations) when the raw file is missing/too large.
    """
    path: Path | None = None
    source_agent = ""
    if agent in (None, "claude"):
        path = find_claude_file(session_id)
        if path is not None:
            source_agent = "claude"
    if path is None and agent in (None, "codex"):
        from . import codex_rollout
        path = codex_rollout.find_rollout_by_session_id(session_id)
        if path is not None:
            source_agent = "codex"
    if path is None:
        return _degraded(store, session_id, f"raw transcript not found for session {session_id}")
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return _degraded(store, session_id, f"transcript too large to render ({path.stat().st_size} bytes)")
    except OSError as e:
        return _degraded(store, session_id, f"cannot read transcript: {e}")

    if source_agent == "claude":
        parsed = parse_claude_transcript(path, max_messages=MAX_MESSAGES)
    else:
        parsed = parse_codex_transcript(path, max_messages=MAX_MESSAGES)
    return {"available": True, "source": {"agent": source_agent, "path": str(path)}, **parsed}
```

Note: `parse_codex_transcript` lands in Task 9. Until then, guard the codex branch so Phase 1 imports cleanly — add a temporary stub at the bottom of the module that Task 9 replaces:

```python
def parse_codex_transcript(path: Path, *, max_messages: int = 2000) -> dict[str, Any]:
    raise NotImplementedError("codex transcript parsing lands in Task 9")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -k load_transcript -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run full backend gates**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -q && .venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: all green. (Import `KBStore` under `TYPE_CHECKING` and quote the annotation to avoid a runtime cycle; `_read_observations` is a private-but-stable helper already used across the package.)

- [ ] **Step 6: Commit**

```bash
git add src/vouch/transcript.py tests/test_session_transcript.py
git commit -m "feat(transcript): orchestrate load with observation fallback"
```

---

### Task 4: `kb.session_transcript` RPC handler

**Files:**
- Modify: `src/vouch/jsonl_server.py` (add handler near `_h_list_sessions` ~line 410; register in `HANDLERS` dict ~line 787)
- Modify: `src/vouch/capabilities.py` (add to `METHODS` list ~line 70, after `"kb.list_sessions"`)
- Test: `tests/test_session_transcript.py`

**Interfaces:**
- Consumes: `transcript.load_transcript`, `_store()`.
- Produces: RPC method `kb.session_transcript(session_id, agent?)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_session_transcript.py
from vouch.capabilities import capabilities
from vouch.jsonl_server import HANDLERS, handle_request


def test_capabilities_advertises_session_transcript() -> None:
    assert "kb.session_transcript" in capabilities().methods
    assert "kb.session_transcript" in HANDLERS


def test_handler_missing_session_id_is_missing_param() -> None:
    resp = handle_request({"id": "1", "method": "kb.session_transcript", "params": {}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "missing_param"


def test_handler_bad_agent_is_invalid_request() -> None:
    resp = handle_request({"id": "2", "method": "kb.session_transcript",
                           "params": {"session_id": "11111111-1111-1111-1111-111111111111", "agent": "grok"}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "invalid_request"


def test_handler_returns_degraded_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = handle_request({"id": "3", "method": "kb.session_transcript",
                           "params": {"session_id": "11111111-1111-1111-1111-111111111111"}})
    assert resp["ok"] is True
    assert resp["result"]["available"] is False
```

Note: `test_handler_returns_degraded_when_absent` runs against the real KB discovered by `_store()`. It asserts only the shape (degraded), which holds regardless of on-disk files, because that id will not exist.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -k "handler or advertises" -q`
Expected: FAIL — `test_capabilities_advertises_session_transcript` fails and `test_capabilities_matches_jsonl_handlers` (existing) fails once you add to only one list; handler tests fail with `method_not_found`.

- [ ] **Step 3: Write minimal implementation**

In `src/vouch/capabilities.py`, add to the `METHODS` list right after `"kb.list_sessions",`:

```python
    "kb.session_transcript",
```

In `src/vouch/jsonl_server.py`, add the handler (place it just after `_h_list_sessions`):

```python
def _h_session_transcript(p: dict) -> dict:
    from . import transcript
    session_id = p["session_id"]
    agent = p.get("agent")
    if agent is not None and agent not in ("claude", "codex"):
        raise ValueError(f"unknown agent: {agent!r} (expected 'claude' or 'codex')")
    return transcript.load_transcript(_store(), session_id, agent=agent)
```

Register it in the `HANDLERS` dict, right after the `"kb.list_sessions": _h_list_sessions,` line:

```python
    "kb.session_transcript": _h_session_transcript,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -q`
Expected: PASS (all). Also run `.venv/bin/python -m pytest tests/test_capabilities.py -q` → PASS.

- [ ] **Step 5: Full backend gates**

Run: `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings && .venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/vouch/jsonl_server.py src/vouch/capabilities.py tests/test_session_transcript.py
git commit -m "feat(transcript): expose kb.session_transcript RPC"
```

---

## PHASE 2 — Frontend: rendering + Sessions view

Work from `webapp/`. Run `npm install` once if `node_modules` is absent.

### Task 5: Transcript client types + fetch

**Files:**
- Create: `webapp/src/lib/transcript.ts`
- Test: `webapp/src/lib/transcript.test.ts`

**Interfaces:**
- Produces: types `TranscriptBlock`, `TranscriptMessage`, `SessionMeta`, `Transcript` (union of available/degraded); `fetchTranscript(conn, sessionId, agent?) -> Promise<Transcript>`.

- [ ] **Step 1: Write the failing test**

```ts
// webapp/src/lib/transcript.test.ts
import { describe, expect, it, vi } from 'vitest'

vi.mock('./rpc', () => ({ rpc: vi.fn() }))
import { rpc } from './rpc'
import { fetchTranscript } from './transcript'
import type { VouchConnectionInfo } from './types'

const conn: VouchConnectionInfo = { endpoint: 'http://127.0.0.1:8731' }

describe('fetchTranscript', () => {
  it('calls kb.session_transcript with session id + agent', async () => {
    vi.mocked(rpc).mockResolvedValue({ available: false, reason: 'x', observations: [] })
    await fetchTranscript(conn, 'sid-1', 'claude')
    expect(rpc).toHaveBeenCalledWith(conn, 'kb.session_transcript', { session_id: 'sid-1', agent: 'claude' })
  })

  it('omits agent when not given', async () => {
    vi.mocked(rpc).mockResolvedValue({ available: false, reason: 'x', observations: [] })
    await fetchTranscript(conn, 'sid-2')
    expect(rpc).toHaveBeenCalledWith(conn, 'kb.session_transcript', { session_id: 'sid-2' })
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- transcript.test.ts`
Expected: FAIL (module `./transcript` not found).

- [ ] **Step 3: Write minimal implementation**

```ts
// webapp/src/lib/transcript.ts
import { rpc } from './rpc'
import type { VouchConnectionInfo } from './types'

export interface Tokens { input: number; output: number; cache_read: number; cache_creation: number }

export interface ToolResult {
  content: string
  is_error: boolean
  subagent_session_id: string | null
}

export type TranscriptBlock =
  | { type: 'text'; text: string }
  | { type: 'thinking'; text: string }
  | { type: 'tool_use'; id: string | null; name: string; input: Record<string, unknown>; result: ToolResult | null }

export interface TranscriptMessage {
  role: 'user' | 'assistant'
  id: string | null
  model: string | null
  timestamp: string | null
  tokens: Tokens | null
  blocks: TranscriptBlock[]
}

export interface SessionMeta {
  id: string
  agent: string
  cwd: string | null
  git_branch: string | null
  title: string | null
  started_at: string | null
  ended_at: string | null
  model: string | null
  tokens: Tokens
}

export interface Observation { ts: number; tool: string; summary: string; files?: string[]; cmd?: string }

export type Transcript =
  | { available: true; source: { agent: string; path: string }; session: SessionMeta; messages: TranscriptMessage[]; truncated: boolean }
  | { available: false; reason: string; observations: Observation[] }

export function fetchTranscript(
  conn: VouchConnectionInfo,
  sessionId: string,
  agent?: string,
): Promise<Transcript> {
  const params: Record<string, unknown> = { session_id: sessionId }
  if (agent) params.agent = agent
  return rpc<Transcript>(conn, 'kb.session_transcript', params)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- transcript.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/lib/transcript.ts webapp/src/lib/transcript.test.ts
git commit -m "feat(webapp): transcript client types and fetch"
```

---

### Task 6: Leaf block renderers (DiffView, CodeBlock, ThinkingBlock)

**Files:**
- Create: `webapp/src/components/transcript/DiffView.tsx`, `CodeBlock.tsx`, `ThinkingBlock.tsx`
- Test: `webapp/src/components/transcript/blocks.test.tsx`

**Interfaces:**
- Produces: `<DiffView text={string} />`; `<CodeBlock code={string} lang?={string} />`; `<ThinkingBlock text={string} />`.

- [ ] **Step 1: Write the failing test**

```tsx
// webapp/src/components/transcript/blocks.test.tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { DiffView } from './DiffView'
import { ThinkingBlock } from './ThinkingBlock'

describe('DiffView', () => {
  it('tags added and removed lines', () => {
    const { container } = render(<DiffView text={'@@ -1 +1 @@\n-old\n+new\n ctx'} />)
    expect(container.querySelector('.diff-add')?.textContent).toContain('+new')
    expect(container.querySelector('.diff-del')?.textContent).toContain('-old')
    expect(container.querySelector('.diff-hunk')?.textContent).toContain('@@')
  })
})

describe('ThinkingBlock', () => {
  it('is collapsed by default and expands on click', async () => {
    render(<ThinkingBlock text="secret reasoning" />)
    expect(screen.queryByText('secret reasoning')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /thinking/i }))
    expect(screen.getByText('secret reasoning')).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- blocks.test.tsx`
Expected: FAIL (modules not found).

- [ ] **Step 3: Write minimal implementation**

```tsx
// webapp/src/components/transcript/DiffView.tsx
export function DiffView({ text }: { text: string }) {
  const lines = text.replace(/\n$/, '').split('\n')
  return (
    <div className="overflow-x-auto rounded-lg border border-rule bg-paper font-mono text-xs">
      {lines.map((line, i) => {
        const cls = line.startsWith('@@')
          ? 'diff-hunk text-accent-2'
          : line.startsWith('+')
            ? 'diff-add bg-ok/10 text-ok'
            : line.startsWith('-')
              ? 'diff-del bg-accent/10 text-accent-2'
              : 'diff-ctx text-ink-2'
        return (
          <div key={i} className={`whitespace-pre px-3 py-0.5 ${cls}`}>
            {line || ' '}
          </div>
        )
      })}
    </div>
  )
}
```

```tsx
// webapp/src/components/transcript/CodeBlock.tsx
export function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-rule bg-paper">
      {lang && (
        <div className="border-b border-rule px-3 py-1 font-mono text-[10px] uppercase tracking-widest text-sepia">
          {lang}
        </div>
      )}
      <pre className="px-3 py-2 font-mono text-xs text-ink-2"><code>{code}</code></pre>
    </div>
  )
}
```

```tsx
// webapp/src/components/transcript/ThinkingBlock.tsx
import { Brain, ChevronRight } from 'lucide-react'
import { useState } from 'react'

export function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-lg border border-rule/60 bg-paper-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left font-mono text-[10px] uppercase tracking-widest text-sepia"
      >
        <ChevronRight size={12} className={`transition ${open ? 'rotate-90' : ''}`} />
        <Brain size={12} /> Thinking
      </button>
      {open && <div className="whitespace-pre-wrap px-3 pb-2 text-xs italic text-sepia">{text}</div>}
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- blocks.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/src/components/transcript/DiffView.tsx webapp/src/components/transcript/CodeBlock.tsx webapp/src/components/transcript/ThinkingBlock.tsx webapp/src/components/transcript/blocks.test.tsx
git commit -m "feat(webapp): thinking, diff, and code block renderers"
```

---

### Task 7: ToolBlock (per-tool rendering + collapsible + result)

**Files:**
- Create: `webapp/src/components/transcript/ToolBlock.tsx`
- Test: `webapp/src/components/transcript/ToolBlock.test.tsx`

**Interfaces:**
- Consumes: `DiffView`, `CodeBlock`, block type from `lib/transcript`, optional `onOpenSubagent(sessionId)` callback.
- Produces: `<ToolBlock block={ToolUseBlock} onOpenSubagent?={(id: string) => void} />`.

- [ ] **Step 1: Write the failing test**

```tsx
// webapp/src/components/transcript/ToolBlock.test.tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ToolBlock } from './ToolBlock'
import type { TranscriptBlock } from '../../lib/transcript'

function tool(over: Partial<Extract<TranscriptBlock, { type: 'tool_use' }>>): Extract<TranscriptBlock, { type: 'tool_use' }> {
  return { type: 'tool_use', id: 't1', name: 'Bash', input: {}, result: null, ...over }
}

describe('ToolBlock', () => {
  it('shows the tool name and a Bash command header', () => {
    render(<ToolBlock block={tool({ name: 'Bash', input: { command: 'go test ./...' } })} />)
    expect(screen.getByText('Bash')).toBeInTheDocument()
    expect(screen.getByText('go test ./...')).toBeInTheDocument()
  })

  it('renders a diff for Edit results and reveals output on expand', async () => {
    const block = tool({
      name: 'Edit',
      input: { file_path: '/x.go' },
      result: { content: '@@ -1 +1 @@\n-a\n+b', is_error: false, subagent_session_id: null },
    })
    const { container } = render(<ToolBlock block={block} />)
    await userEvent.click(screen.getByRole('button', { name: /Edit/i }))
    expect(container.querySelector('.diff-add')).not.toBeNull()
  })

  it('marks errored results', async () => {
    const block = tool({ result: { content: 'boom', is_error: true, subagent_session_id: null } })
    render(<ToolBlock block={block} />)
    await userEvent.click(screen.getByRole('button', { name: /Bash/i }))
    expect(screen.getByText('boom')).toBeInTheDocument()
    expect(screen.getByTestId('tool-error')).toBeInTheDocument()
  })

  it('offers a subagent link and fires the callback', async () => {
    const onOpen = vi.fn()
    const block = tool({ name: 'Task', input: { subagent_type: 'Explore', prompt: 'find x' },
      result: { content: 'done', is_error: false, subagent_session_id: 'child-9' } })
    render(<ToolBlock block={block} onOpenSubagent={onOpen} />)
    await userEvent.click(screen.getByRole('button', { name: /view subagent/i }))
    expect(onOpen).toHaveBeenCalledWith('child-9')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- ToolBlock.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Write minimal implementation**

```tsx
// webapp/src/components/transcript/ToolBlock.tsx
import { ChevronRight, CornerDownRight, Wrench } from 'lucide-react'
import { useState } from 'react'
import type { TranscriptBlock } from '../../lib/transcript'
import { CodeBlock } from './CodeBlock'
import { DiffView } from './DiffView'

type ToolUse = Extract<TranscriptBlock, { type: 'tool_use' }>

/** One-line summary of the tool's input, agentsview-style. */
function headline(block: ToolUse): string {
  const i = block.input as Record<string, unknown>
  const s = (k: string) => (typeof i[k] === 'string' ? (i[k] as string) : '')
  switch (block.name) {
    case 'Bash':
    case 'run_command':
      return s('command') || s('cmd')
    case 'Read':
    case 'Edit':
    case 'MultiEdit':
    case 'Write':
    case 'Update':
    case 'NotebookEdit':
      return s('file_path') || s('path') || s('notebook_path')
    case 'Grep':
      return s('pattern')
    case 'Glob':
      return s('pattern') || s('glob')
    case 'Task':
    case 'Agent':
      return s('subagent_type') || s('description') || s('prompt').slice(0, 80)
    default:
      return ''
  }
}

function ResultBody({ block }: { block: ToolUse }) {
  const r = block.result
  if (!r) return <p className="px-1 text-xs italic text-sepia">no output captured</p>
  const isEdit = ['Edit', 'MultiEdit', 'Write', 'Update'].includes(block.name)
  if (isEdit && /^@@|\n[+-]/.test(r.content)) return <DiffView text={r.content} />
  if (r.is_error) {
    return (
      <pre data-testid="tool-error" className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-accent/40 bg-accent/10 px-3 py-2 text-xs text-accent-2">
        {r.content}
      </pre>
    )
  }
  return <CodeBlock code={r.content || '(empty)'} />
}

export function ToolBlock({
  block,
  onOpenSubagent,
}: {
  block: ToolUse
  onOpenSubagent?: (sessionId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const head = headline(block)
  const child = block.result?.subagent_session_id ?? null
  return (
    <div className="rounded-lg border border-rule bg-paper-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
      >
        <ChevronRight size={12} className={`shrink-0 text-sepia transition ${open ? 'rotate-90' : ''}`} />
        <Wrench size={12} className="shrink-0 text-accent" />
        <span className="shrink-0 font-mono text-[11px] font-semibold text-ink">{block.name}</span>
        {head && <span className="truncate font-mono text-[11px] text-sepia">{head}</span>}
        {block.result?.is_error && <span className="ml-auto text-[10px] font-bold text-accent">ERROR</span>}
      </button>
      {open && (
        <div className="space-y-2 px-3 pb-2">
          {Object.keys(block.input).length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-widest text-sepia">input</summary>
              <CodeBlock code={JSON.stringify(block.input, null, 2)} lang="json" />
            </details>
          )}
          <ResultBody block={block} />
          {child && onOpenSubagent && (
            <button
              onClick={() => onOpenSubagent(child)}
              className="flex items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] text-accent-2 transition hover:bg-accent/20"
            >
              <CornerDownRight size={12} /> view subagent
            </button>
          )}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- ToolBlock.test.tsx`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add webapp/src/components/transcript/ToolBlock.tsx webapp/src/components/transcript/ToolBlock.test.tsx
git commit -m "feat(webapp): tool block with per-tool rendering and diffs"
```

---

### Task 8: MessageBlock + TranscriptView + SessionsView + route/nav

**Files:**
- Create: `webapp/src/components/transcript/MessageBlock.tsx`
- Create: `webapp/src/views/TranscriptView.tsx`
- Create: `webapp/src/views/SessionsView.tsx`
- Modify: `webapp/src/App.tsx` (add route)
- Modify: `webapp/src/components/Shell.tsx` (add nav item + title)
- Test: `webapp/src/views/SessionsView.test.tsx`

**Interfaces:**
- Consumes: `ThinkingBlock`, `ToolBlock`, `Markdown`, `fetchTranscript`, `useConnection`, `useFanout`, `SessionEntry`.
- Produces: `<MessageBlock message={TranscriptMessage} onOpenSubagent? />`; `<TranscriptView conn={VouchConnectionInfo} sessionId={string} agent?={string} />`; `<SessionsView />`.

- [ ] **Step 1: Write the failing test**

```tsx
// webapp/src/views/SessionsView.test.tsx
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { SessionsView } from './SessionsView'

const CAPS = { name: 'vouch', version: '1', level: 3, methods: ['kb.list_sessions', 'kb.session_transcript'], review_gated: true }

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS as never)
  seedConnection()
})

describe('SessionsView', () => {
  it('lists sessions and renders a picked transcript', async () => {
    vi.mocked(rpc).mockImplementation(async (_c, method) => {
      if (method === 'kb.list_sessions') {
        return { sessions: [{ session_id: 'sid-1', stage: 'buffer', proposal_id: null, kind: null,
          title: 'Fix parser', summarized: false, observations: 3, last_activity: '2026-07-10T00:00:00Z' }] }
      }
      if (method === 'kb.session_transcript') {
        return { available: true, source: { agent: 'claude', path: '/x' },
          session: { id: 'sid-1', agent: 'claude', cwd: '/repo', git_branch: 'main', title: 'Fix parser',
            started_at: null, ended_at: null, model: 'claude-opus-4-8',
            tokens: { input: 1, output: 1, cache_read: 0, cache_creation: 0 } },
          messages: [{ role: 'assistant', id: 'm1', model: 'claude-opus-4-8', timestamp: null, tokens: null,
            blocks: [{ type: 'text', text: 'hello from claude' }] }], truncated: false }
      }
      return {}
    })
    renderWithProviders(<SessionsView />, { route: '/sessions' })
    await waitFor(() => expect(screen.getByText('Fix parser')).toBeInTheDocument())
    await userEvent.click(screen.getByText('Fix parser'))
    await waitFor(() => expect(screen.getByText('hello from claude')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test -- SessionsView.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Write minimal implementation**

```tsx
// webapp/src/components/transcript/MessageBlock.tsx
import { Bot, User } from 'lucide-react'
import { Markdown } from '../Markdown'
import type { TranscriptMessage } from '../../lib/transcript'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolBlock } from './ToolBlock'

export function MessageBlock({
  message,
  onOpenSubagent,
}: {
  message: TranscriptMessage
  onOpenSubagent?: (sessionId: string) => void
}) {
  const isUser = message.role === 'user'
  return (
    <div className="rounded-xl border border-rule bg-paper-2 p-3">
      <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-sepia">
        {isUser ? <User size={12} /> : <Bot size={12} className="text-accent" />}
        <span>{isUser ? 'user' : 'assistant'}</span>
        {message.model && <span className="text-ink-2">{message.model}</span>}
      </div>
      <div className="space-y-2">
        {message.blocks.map((b, i) => {
          if (b.type === 'thinking') return <ThinkingBlock key={i} text={b.text} />
          if (b.type === 'tool_use') return <ToolBlock key={i} block={b} onOpenSubagent={onOpenSubagent} />
          return (
            <div key={i} className="markdown-body text-sm text-ink">
              <Markdown>{b.text}</Markdown>
            </div>
          )
        })}
      </div>
    </div>
  )
}
```

```tsx
// webapp/src/views/TranscriptView.tsx
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { MessageBlock } from '../components/transcript/MessageBlock'
import { fetchTranscript } from '../lib/transcript'
import type { Observation } from '../lib/transcript'
import type { VouchConnectionInfo } from '../lib/types'
import { VouchRpcError } from '../lib/rpc'

function Degraded({ reason, observations }: { reason: string; observations: Observation[] }) {
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-rule/60 bg-paper-2 px-3 py-2 text-xs text-sepia">
        original transcript unavailable — {reason}. Showing captured activity.
      </div>
      {observations.length === 0 ? (
        <EmptyState title="No captured activity" />
      ) : (
        <ol className="space-y-1">
          {observations.map((o, i) => (
            <li key={i} className="flex items-center gap-2 rounded-lg border border-rule bg-paper-2 px-3 py-1.5 text-xs">
              <span className="font-mono text-[11px] font-semibold text-accent">{o.tool}</span>
              <span className="text-ink-2">{o.summary}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

export function TranscriptView({
  conn,
  sessionId,
  agent,
}: {
  conn: VouchConnectionInfo
  sessionId: string
  agent?: string
}) {
  // Subagent drill-down replaces the shown transcript with the child's, with a back stack.
  const [stack, setStack] = useState<{ id: string; agent?: string }[]>([{ id: sessionId, agent }])
  const top = stack[stack.length - 1]
  const q = useQuery({
    queryKey: ['transcript', conn.endpoint, top.id],
    queryFn: () => fetchTranscript(conn, top.id, top.agent),
  })

  if (q.isPending) return <div className="p-6 text-sm text-sepia">Loading transcript…</div>
  if (q.isError) {
    const e = q.error
    return <div className="p-6"><ErrorCard code={e instanceof VouchRpcError ? e.code : undefined} message={e instanceof Error ? e.message : String(e)} /></div>
  }
  const t = q.data
  return (
    <div className="space-y-3 p-4">
      {stack.length > 1 && (
        <button onClick={() => setStack((s) => s.slice(0, -1))} className="text-xs text-accent-2 hover:underline">
          ← back to parent session
        </button>
      )}
      {!t.available ? (
        <Degraded reason={t.reason} observations={t.observations} />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-rule bg-paper-2 px-4 py-2 font-mono text-[11px] text-sepia">
            {t.session.model && <span className="text-ink-2">{t.session.model}</span>}
            {t.session.cwd && <span>{t.session.cwd}</span>}
            {t.session.git_branch && <span>⎇ {t.session.git_branch}</span>}
            <span>{t.session.tokens.input + t.session.tokens.output} tokens</span>
            <span className="uppercase tracking-widest">{t.source.agent}</span>
          </div>
          {t.truncated && (
            <div className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs text-accent-2">
              transcript truncated at {t.messages.length} messages
            </div>
          )}
          {t.messages.map((m, i) => (
            <MessageBlock key={i} message={m} onOpenSubagent={(id) => setStack((s) => [...s, { id, agent: t.source.agent }])} />
          ))}
        </>
      )}
    </div>
  )
}
```

```tsx
// webapp/src/views/SessionsView.tsx
import { useState } from 'react'
import { EmptyState } from '../components/EmptyState'
import { useConnection } from '../connection/ConnectionContext'
import { useFanout } from '../lib/fanout'
import type { SessionEntry } from '../lib/types'
import type { VouchConnectionInfo } from '../lib/types'
import { TranscriptView } from './TranscriptView'

interface Row { conn: VouchConnectionInfo; label: string; s: SessionEntry }

export function SessionsView() {
  const { hasMethod } = useConnection()
  const sessions = useFanout<{ sessions: SessionEntry[] }>(['sessions'], 'kb.list_sessions', {}, {
    refetchInterval: 10_000,
  })
  const rows: Row[] = sessions.rows.flatMap((r) =>
    (r.data?.sessions ?? []).map((s) => ({ conn: r.project.conn, label: r.project.label, s })),
  )
  const [sel, setSel] = useState<Row | null>(null)

  return (
    <div className="flex h-full">
      <aside className="w-72 shrink-0 overflow-y-auto border-r border-rule">
        {rows.length === 0 ? (
          <div className="p-4"><EmptyState title="No sessions" hint="Captured agent sessions will appear here." /></div>
        ) : (
          <ul>
            {rows.map((row, i) => {
              const openable = !!row.s.session_id && hasMethod('kb.session_transcript', row.conn.endpoint)
              const active = sel?.s.session_id === row.s.session_id && sel?.conn.endpoint === row.conn.endpoint
              return (
                <li key={`${row.conn.endpoint}-${row.s.session_id ?? i}`}>
                  <button
                    disabled={!openable}
                    onClick={() => setSel(row)}
                    className={`block w-full border-b border-rule/60 px-4 py-2.5 text-left transition ${
                      active ? 'bg-paper-3' : 'hover:bg-paper-2'
                    } ${openable ? '' : 'cursor-not-allowed opacity-50'}`}
                  >
                    <div className="truncate text-sm text-ink">{row.s.title ?? row.s.session_id ?? 'untitled session'}</div>
                    <div className="mt-0.5 flex items-center gap-2 font-mono text-[10px] text-sepia">
                      <span className="uppercase">{row.s.stage}</span>
                      {row.s.observations != null && <span>{row.s.observations} obs</span>}
                    </div>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </aside>
      <section className="min-w-0 flex-1 overflow-y-auto">
        {sel && sel.s.session_id ? (
          <TranscriptView conn={sel.conn} sessionId={sel.s.session_id} />
        ) : (
          <div className="p-6"><EmptyState title="Select a session" hint="Pick a session to view its full transcript." /></div>
        )}
      </section>
    </div>
  )
}
```

In `webapp/src/App.tsx`, add the import and route (after the `/stats` route):

```tsx
import { SessionsView } from './views/SessionsView'
// ...
                <Route path="/sessions" element={<SessionsView />} />
```

In `webapp/src/components/Shell.tsx`, import an icon and add nav + title:

```tsx
// add ScrollText to the lucide-react import
import { Activity, BadgeCheck, FileClock, Inbox, Library, MessageSquare, Plug, ScrollText, SunMoon } from 'lucide-react'
// add to NAV array (before /stats):
  { to: '/sessions', label: 'Sessions', icon: ScrollText },
// add to TITLES:
  '/sessions': 'Session transcripts',
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test -- SessionsView.test.tsx`
Expected: PASS.

- [ ] **Step 5: Full frontend gates**

Run: `npm run test && npm run build`
Expected: all tests pass; `tsc && vite build` succeeds with no type errors.

- [ ] **Step 6: Commit**

```bash
git add webapp/src/components/transcript/MessageBlock.tsx webapp/src/views/TranscriptView.tsx webapp/src/views/SessionsView.tsx webapp/src/views/SessionsView.test.tsx webapp/src/App.tsx webapp/src/components/Shell.tsx
git commit -m "feat(webapp): sessions tab with full transcript viewer"
```

---

## PHASE 3 — Codex, subagents polish, e2e

### Task 9: Codex full-transcript parser

**Files:**
- Modify: `src/vouch/transcript.py` (replace the `parse_codex_transcript` stub)
- Test: `tests/test_session_transcript.py`

**Interfaces:**
- Consumes: `codex_rollout._iter_rollout_lines` is NOT reused (it raises on zstd); parse plain records directly.
- Produces: `parse_codex_transcript(path, *, max_messages=2000) -> dict[str, Any]` returning the same schema, `session.agent == "codex"`.

Codex rollout records (verified in `codex_rollout.py`): `{"type":"session_meta","payload":{"id","cwd","timestamp"}}`; `{"type":"event_msg","payload":{"type":"user_message","message":"…"}}` and `agent_message` (assistant text, field `message`); `{"type":"response_item","payload":{"type":"function_call","name","arguments","call_id"}}` and `{"type":"response_item","payload":{"type":"function_call_output","call_id","output"}}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_session_transcript.py
_CODEX_LINES = [
    {"type": "session_meta", "payload": {"id": "cx-1", "cwd": "/repo", "timestamp": "2026-06-22T08:01:54Z"}},
    {"type": "event_msg", "payload": {"type": "user_message", "message": "run the tests"}},
    {"type": "event_msg", "payload": {"type": "agent_message", "message": "Running them now."}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                          "arguments": "{\"command\": \"pytest\"}", "call_id": "c1"}},
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
                                          "output": "1 passed"}},
]


def test_parse_codex_pairs_calls(tmp_path: Path) -> None:
    f = tmp_path / "rollout-x.jsonl"
    _write_jsonl(f, _CODEX_LINES)
    out = transcript.parse_codex_transcript(f)
    assert out["session"]["agent"] == "codex"
    assert out["session"]["cwd"] == "/repo"
    roles = [m["role"] for m in out["messages"]]
    assert roles[0] == "user"
    # the assistant message carries the agent text and the paired tool call
    assistant = next(m for m in out["messages"] if m["role"] == "assistant")
    types = [b["type"] for b in assistant["blocks"]]
    assert "tool_use" in types and "text" in types
    tu = next(b for b in assistant["blocks"] if b["type"] == "tool_use")
    assert tu["name"] == "shell"
    assert tu["result"]["content"] == "1 passed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -k codex -q`
Expected: FAIL — `NotImplementedError` from the stub.

- [ ] **Step 3: Write minimal implementation** (replace the stub)

```python
def parse_codex_transcript(path: Path, *, max_messages: int = 2000) -> dict[str, Any]:
    """Parse a Codex rollout JSONL into the normalized transcript schema.

    Codex interleaves user/agent messages (``event_msg``) with tool calls
    and outputs (``response_item``). Consecutive assistant activity — agent
    text and its function calls — is grouped into one assistant message;
    a ``function_call_output`` pairs into its call by ``call_id``.
    """
    session: dict[str, Any] = {
        "id": path.stem, "agent": "codex", "cwd": None, "git_branch": None,
        "title": None, "started_at": None, "ended_at": None, "model": None,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    messages: list[dict[str, Any]] = []
    tool_by_call: dict[str, dict[str, Any]] = {}
    truncated = False
    current: dict[str, Any] | None = None

    def new_assistant() -> dict[str, Any]:
        return {"role": "assistant", "id": None, "model": None, "timestamp": None,
                "tokens": None, "blocks": []}

    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            rtype = rec.get("type")
            if len(messages) >= max_messages:
                truncated = True
                break

            if rtype == "session_meta":
                if isinstance(payload.get("id"), str) and session["id"] == path.stem:
                    session["id"] = payload["id"]
                if isinstance(payload.get("cwd"), str):
                    session["cwd"] = payload["cwd"]
                if isinstance(payload.get("timestamp"), str):
                    session["started_at"] = payload["timestamp"]
                    session["ended_at"] = payload["timestamp"]
            elif rtype == "event_msg" and payload.get("type") == "user_message":
                if current is not None:
                    messages.append(current)
                    current = None
                text = str(payload.get("message", "")).strip()
                if text:
                    messages.append({"role": "user", "id": None, "model": None,
                                     "timestamp": None, "tokens": None,
                                     "blocks": [{"type": "text", "text": text}]})
            elif rtype == "event_msg" and payload.get("type") == "agent_message":
                if current is None:
                    current = new_assistant()
                text = str(payload.get("message", "")).strip()
                if text:
                    current["blocks"].append({"type": "text", "text": text})
            elif rtype == "response_item" and payload.get("type") == "function_call":
                if current is None:
                    current = new_assistant()
                block = {"type": "tool_use", "id": payload.get("call_id"),
                         "name": str(payload.get("name", "")),
                         "input": {"arguments": payload.get("arguments")}, "result": None}
                current["blocks"].append(block)
                cid = payload.get("call_id")
                if isinstance(cid, str):
                    tool_by_call[cid] = block
            elif rtype == "response_item" and payload.get("type") == "function_call_output":
                cid = payload.get("call_id")
                block = tool_by_call.get(cid) if isinstance(cid, str) else None
                if block is not None:
                    block["result"] = {"content": str(payload.get("output", "")),
                                       "is_error": False, "subagent_session_id": None}
    if current is not None:
        messages.append(current)
    return {"session": session, "messages": messages, "truncated": truncated}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_session_transcript.py -k codex -q`
Expected: PASS.

- [ ] **Step 5: Full backend gates + commit**

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings && .venv/bin/python -m mypy src && .venv/bin/python -m ruff check src tests
git add src/vouch/transcript.py tests/test_session_transcript.py
git commit -m "feat(transcript): parse Codex rollouts into normalized blocks"
```

---

### Task 10: Degraded-render + subagent test coverage (frontend)

**Files:**
- Create: `webapp/src/views/TranscriptView.test.tsx`

**Interfaces:** consumes existing `TranscriptView`.

- [ ] **Step 1: Write the failing test**

```tsx
// webapp/src/views/TranscriptView.test.tsx
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { rpc } from '../lib/rpc'
import { renderWithProviders } from '../test/utils'
import { TranscriptView } from './TranscriptView'

const conn = { endpoint: 'http://127.0.0.1:8731' }

beforeEach(() => { vi.clearAllMocks() })

describe('TranscriptView', () => {
  it('renders the degraded observation timeline', async () => {
    vi.mocked(rpc).mockResolvedValue({ available: false, reason: 'raw transcript not found',
      observations: [{ ts: 1, tool: 'Edit', summary: 'Edited types.go' }] })
    renderWithProviders(<TranscriptView conn={conn} sessionId="sid-x" />)
    await waitFor(() => expect(screen.getByText('Edited types.go')).toBeInTheDocument())
    expect(screen.getByText(/original transcript unavailable/i)).toBeInTheDocument()
  })

  it('drills into a subagent and back', async () => {
    vi.mocked(rpc).mockImplementation(async (_c, _m, params) => {
      const id = (params as { session_id: string }).session_id
      const text = id === 'child-9' ? 'child says hi' : 'parent turn'
      const blocks = id === 'child-9'
        ? [{ type: 'text', text }]
        : [{ type: 'tool_use', id: 't1', name: 'Task', input: { prompt: 'go' },
             result: { content: 'done', is_error: false, subagent_session_id: 'child-9' } }]
      return { available: true, source: { agent: 'claude', path: '/x' },
        session: { id, agent: 'claude', cwd: null, git_branch: null, title: null, started_at: null,
          ended_at: null, model: null, tokens: { input: 0, output: 0, cache_read: 0, cache_creation: 0 } },
        messages: [{ role: 'assistant', id: 'm', model: null, timestamp: null, tokens: null, blocks }],
        truncated: false }
    })
    renderWithProviders(<TranscriptView conn={conn} sessionId="parent-1" />)
    await waitFor(() => expect(screen.getByText('Task')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /Task/i }))
    await userEvent.click(screen.getByRole('button', { name: /view subagent/i }))
    await waitFor(() => expect(screen.getByText('child says hi')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /back to parent/i }))
    await waitFor(() => expect(screen.getByText('Task')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `npm run test -- TranscriptView.test.tsx`
Expected: These should PASS against the Task 8 implementation. If the subagent-back or degraded rendering fails, fix `TranscriptView` (this task is the regression net that proves both paths). Do not modify tests to fit bugs.

- [ ] **Step 3: Full frontend gates + commit**

```bash
npm run test && npm run build
git add webapp/src/views/TranscriptView.test.tsx
git commit -m "test(webapp): cover degraded render and subagent drill-down"
```

---

### Task 11: E2E smoke

**Files:**
- Create: `webapp/e2e/sessions.spec.ts`

**Interfaces:** exercises the running app against the existing e2e fixture server (see `webapp/e2e/global-setup.ts`).

- [ ] **Step 1: Inspect the existing e2e harness**

Run: `sed -n '1,60p' webapp/e2e/smoke.spec.ts webapp/e2e/global-setup.ts`
Learn how the fixture endpoint is seeded and how `kb.*` responses are stubbed/served, then mirror that to stub `kb.list_sessions` + `kb.session_transcript`. (The spec must drive the real UI — navigate to Sessions, click a row, assert a rendered block — not assert on source.)

- [ ] **Step 2: Write the spec**

```ts
// webapp/e2e/sessions.spec.ts
import { expect, test } from '@playwright/test'

// Follow the pattern established in smoke.spec.ts for seeding a connection
// and stubbing /proxy/rpc. Route kb.list_sessions -> one row with a
// session_id, and kb.session_transcript -> an available transcript with a
// single assistant text block "e2e transcript body".
test('sessions tab renders a picked transcript', async ({ page }) => {
  // ...seed + route stubs mirroring smoke.spec.ts...
  await page.goto('/sessions')
  await page.getByText('e2e session').click()
  await expect(page.getByText('e2e transcript body')).toBeVisible()
})
```

- [ ] **Step 3: Run it**

Run: `npm run e2e -- sessions.spec.ts`
Expected: PASS. (If the harness cannot stub per-method easily, assert the empty-state path instead: navigating to `/sessions` shows "No sessions" — still real behavior, no tautology.)

- [ ] **Step 4: Commit**

```bash
git add webapp/e2e/sessions.spec.ts
git commit -m "test(webapp): e2e smoke for the sessions transcript tab"
```

---

### Task 12: Spec sync + docs

**Files:**
- Modify: `docs/superpowers/specs/2026-07-10-session-transcript-viewer-design.md`

- [ ] **Step 1:** Update the spec's "Backend contract" to reflect server-side tool_result pairing (blocks carry `result` inline; no separate `tool_result` block) and resolve the open questions to what shipped (Sessions tab, master–detail, Codex in v1). Run `mdformat --wrap 80 docs/superpowers/specs/2026-07-10-session-transcript-viewer-design.md` if available.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-07-10-session-transcript-viewer-design.md
git commit -m "docs(transcript): align spec with shipped contract"
```

---

## Self-Review

**Spec coverage:** Backend RPC + `transcript.py` (Tasks 1-4, 9). Locator Claude + Codex (1, 9). Normalized schema (2, 9). Degradation to observations (3, 10). Frontend SessionsView + TranscriptView + block renderers (5-8). Per-tool rendering + diffs (7). Subagent lazy expansion (7, 8, 10). Capability gating (8). Tests every task; e2e (11). Conventions/guardrails in Global Constraints. All spec sections map to a task.

**Placeholder scan:** No TBD/TODO. The only forward reference is `parse_codex_transcript` (stub in Task 3, implemented Task 9) — explicitly flagged with a raising stub so Phase 1 stays green. E2E spec body references the existing harness pattern (Task 11 Step 1 reads it first) rather than inventing an unknown stub API.

**Type consistency:** Block schema identical across backend (dicts) and `lib/transcript.ts` (`TranscriptBlock`). `tool_use.result` is `{content, is_error, subagent_session_id}` everywhere. `fetchTranscript(conn, sessionId, agent?)`, `load_transcript(store, session_id, *, agent=None)`, and the handler param names (`session_id`, `agent`) match. `useFanout` row shape (`{project, data}`) matches Task 8 usage.

---

## Execution Handoff

Recommended: **Subagent-Driven** (fresh subagent per task, two-stage review between tasks). Phase 1 (backend, Tasks 1-4) is the critical path and must be green before Phase 2 consumes the RPC. Alternative: **Inline Execution** with checkpoints after each phase.
