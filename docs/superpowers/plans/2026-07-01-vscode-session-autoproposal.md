# VS Code Session Auto-Proposal Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically finalize old capture buffers on SessionStart and finalize the current session when the VS Code window closes, without user action.

**Architecture:** Add `finalize_all_except()` to capture.py to bulk-finalize stale buffers. Wire it into a new `vouch capture finalize-all` CLI command. Update the SessionStart hook to call this command, catching old sessions. For window close, add a WindowClose hook (if available) or document the fallback behavior.

**Tech Stack:** Python 3.10+, Click CLI framework, existing capture.py, adapter settings.json

## Global Constraints

- No Co-Authored-By trailers in commits (per CLAUDE.md)
- All writes through the review gate (`proposals.approve()` never bypassed)
- Lowercase prose in commit bodies
- Test names follow `test_<module>_<function>` pattern
- All capture failures must not break the session (silent success on errors)

---

## File Structure

| File | Change | Responsibility |
|------|--------|-----------------|
| `src/vouch/capture.py` | Modify | Add `finalize_all_except()`, `is_stale_buffer()` functions |
| `src/vouch/cli.py` | Modify | Add `vouch capture finalize-all` command |
| `adapters/claude-code/.claude/settings.json` | Modify | Wire SessionStart hook to call `finalize-all` |
| `tests/test_capture.py` | Modify | Add 8+ unit tests for new functionality |

---

## Task 1: Add helper functions to capture.py

**Files:**
- Modify: `src/vouch/capture.py:283+`
- Test: `tests/test_capture.py`

**Interfaces:**
- Produces: 
  - `is_stale_buffer(path: Path, age_seconds: float = 3600.0) -> bool`
  - `finalize_all_except(store: KBStore, current_session_id: str, *, max_age_seconds: float = 3600.0, cwd: Path | None = None) -> dict[str, Any]`

- [ ] **Step 1: Write the failing test for `is_stale_buffer()`**

At the end of `tests/test_capture.py`, add:

```python
def test_is_stale_buffer_with_recent_file(tmp_path):
    """Recent file should not be stale."""
    f = tmp_path / "recent.jsonl"
    f.write_text("test")
    now = time.time()
    # File created 30 seconds ago; max_age=3600
    assert not capture_mod.is_stale_buffer(f, max_age_seconds=3600, now_timestamp=now)


def test_is_stale_buffer_with_old_file(tmp_path):
    """File older than max_age should be stale."""
    f = tmp_path / "old.jsonl"
    f.write_text("test")
    old_time = time.time() - 7200  # 2 hours ago
    f.touch(times=(old_time, old_time))  # Set mtime to 2 hours ago
    now = time.time()
    assert capture_mod.is_stale_buffer(f, max_age_seconds=3600, now_timestamp=now)


def test_is_stale_buffer_with_exact_boundary(tmp_path):
    """File at exact max_age boundary should not be stale (>=)."""
    f = tmp_path / "boundary.jsonl"
    f.write_text("test")
    exact_time = time.time() - 3600  # Exactly 1 hour ago
    f.touch(times=(exact_time, exact_time))
    now = exact_time + 3600
    assert not capture_mod.is_stale_buffer(f, max_age_seconds=3600, now_timestamp=now)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/a/Dev/plind-junior/vouch
source .venv/bin/activate
pytest tests/test_capture.py::test_is_stale_buffer_with_recent_file -xvs
```

Expected: `FAILED ... NameError: name 'is_stale_buffer' is not defined`

- [ ] **Step 3: Implement `is_stale_buffer()` in capture.py**

After the `pending_count()` function (line 283), add:

```python
def is_stale_buffer(
    path: Path,
    *,
    max_age_seconds: float = 3600.0,
    now_timestamp: float | None = None,
) -> bool:
    """Check if a buffer file's mtime is older than max_age_seconds."""
    if not path.exists():
        return False
    now = now_timestamp if now_timestamp is not None else time.time()
    mtime = path.stat().st_mtime
    age = now - mtime
    return age > max_age_seconds
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_capture.py::test_is_stale_buffer_with_recent_file tests/test_capture.py::test_is_stale_buffer_with_old_file tests/test_capture.py::test_is_stale_buffer_with_exact_boundary -xvs
```

Expected: All 3 PASS

- [ ] **Step 5: Write failing tests for `finalize_all_except()`**

Add to `tests/test_capture.py`:

```python
def test_finalize_all_except_skips_current_session(tmp_path):
    """Should not finalize the current session buffer."""
    store = _make_store(tmp_path)
    sess_id = "current-session"
    
    # Create a current session buffer with observations
    path = capture_mod.buffer_path(store, sess_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')
    
    result = capture_mod.finalize_all_except(
        store, sess_id, max_age_seconds=3600.0
    )
    
    assert result["skipped_current"] == [sess_id]
    assert path.exists()  # Not removed


def test_finalize_all_except_finalizes_old_buffer(tmp_path):
    """Should finalize buffers older than max_age, except current session."""
    store = _make_store(tmp_path)
    current_sess = "current"
    old_sess = "old-session"
    
    # Create old buffer (2 hours old)
    old_path = capture_mod.buffer_path(store, old_sess)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')
    old_mtime = time.time() - 7200
    old_path.touch(times=(old_mtime, old_mtime))
    
    # Create current buffer (recent)
    curr_path = capture_mod.buffer_path(store, current_sess)
    curr_path.write_text('{"ts": 2.0, "tool": "Write", "summary": "test2"}\n')
    
    result = capture_mod.finalize_all_except(
        store, current_sess, max_age_seconds=3600.0
    )
    
    assert old_sess in result["finalized"]
    assert current_sess in result["skipped_current"]
    assert not old_path.exists()  # Removed after finalize
    assert curr_path.exists()  # Current session untouched


def test_finalize_all_except_skips_recent_buffers(tmp_path):
    """Should not finalize buffers younger than max_age."""
    store = _make_store(tmp_path)
    current_sess = "current"
    recent_sess = "recent-other"
    
    # Create recent buffer (30 minutes old)
    recent_path = capture_mod.buffer_path(store, recent_sess)
    recent_path.parent.mkdir(parents=True, exist_ok=True)
    recent_path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')
    recent_mtime = time.time() - 1800
    recent_path.touch(times=(recent_mtime, recent_mtime))
    
    result = capture_mod.finalize_all_except(
        store, current_sess, max_age_seconds=3600.0
    )
    
    assert recent_sess in result["skipped_recent"]
    assert recent_path.exists()  # Not removed


def test_finalize_all_except_multiple_buffers(tmp_path):
    """Should handle multiple old and recent buffers correctly."""
    store = _make_store(tmp_path)
    current_sess = "current"
    
    # Create 3 old buffers, 2 recent buffers
    old_sesses = ["old1", "old2", "old3"]
    recent_sesses = ["recent1", "recent2"]
    
    now = time.time()
    for sid in old_sesses:
        path = capture_mod.buffer_path(store, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')
        old_mtime = now - 7200  # 2 hours ago
        path.touch(times=(old_mtime, old_mtime))
    
    for sid in recent_sesses:
        path = capture_mod.buffer_path(store, sid)
        path.write_text('{"ts": 2.0, "tool": "Read", "summary": "test"}\n')
        recent_mtime = now - 600  # 10 minutes ago
        path.touch(times=(recent_mtime, recent_mtime))
    
    # Create current session buffer
    curr_path = capture_mod.buffer_path(store, current_sess)
    curr_path.write_text('{"ts": 3.0, "tool": "Write", "summary": "test"}\n')
    
    result = capture_mod.finalize_all_except(
        store, current_sess, max_age_seconds=3600.0, now_timestamp=now
    )
    
    assert set(result["finalized"]) == set(old_sesses)
    assert set(result["skipped_recent"]) == set(recent_sesses)
    assert result["skipped_current"] == [current_sess]
    
    # Verify old buffers are removed, others exist
    for sid in old_sesses:
        assert not capture_mod.buffer_path(store, sid).exists()
    for sid in recent_sesses + [current_sess]:
        assert capture_mod.buffer_path(store, sid).exists()


def test_finalize_all_except_empty_captures_dir(tmp_path):
    """Should handle empty or missing captures directory gracefully."""
    store = _make_store(tmp_path)
    result = capture_mod.finalize_all_except(
        store, "current-session", max_age_seconds=3600.0
    )
    
    assert result["finalized"] == []
    assert result["skipped_recent"] == []
    assert result["skipped_current"] == []


def test_finalize_all_except_returns_proposal_ids(tmp_path):
    """finalize_all_except should return proposal IDs of finalized buffers."""
    store = _make_store(tmp_path)
    old_sess = "old-session"
    current_sess = "current"
    
    # Create old buffer with enough observations
    old_path = capture_mod.buffer_path(store, old_sess)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    observations = [
        '{"ts": 1.0, "tool": "Read", "summary": "test1"}',
        '{"ts": 2.0, "tool": "Read", "summary": "test2"}',
        '{"ts": 3.0, "tool": "Read", "summary": "test3"}',
    ]
    old_path.write_text("\n".join(observations) + "\n")
    old_mtime = time.time() - 7200
    old_path.touch(times=(old_mtime, old_mtime))
    
    # Create current session buffer
    curr_path = capture_mod.buffer_path(store, current_sess)
    curr_path.write_text('{"ts": 4.0, "tool": "Write", "summary": "test"}\n')
    
    result = capture_mod.finalize_all_except(
        store, current_sess, max_age_seconds=3600.0
    )
    
    assert old_sess in result["finalized"]
    # Verify a proposal was created
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) > 0
```

- [ ] **Step 6: Run tests to verify they fail**

```bash
pytest tests/test_capture.py::test_finalize_all_except_skips_current_session -xvs
```

Expected: `FAILED ... NameError: name 'finalize_all_except' is not defined`

- [ ] **Step 7: Implement `finalize_all_except()` in capture.py**

After the `is_stale_buffer()` function, add:

```python
def finalize_all_except(
    store: KBStore,
    current_session_id: str,
    *,
    max_age_seconds: float = 3600.0,
    cwd: Path | None = None,
    now_timestamp: float | None = None,
) -> dict[str, Any]:
    """Finalize all buffers except current_session_id, if they're older than max_age.
    
    Returns dict with keys:
      - finalized: [session_id1, session_id2, ...]  session IDs that were finalized
      - skipped_recent: [id3, id4, ...]  sessions too recent to finalize
      - skipped_current: [id5]  the current session (always skipped)
    """
    finalized: list[str] = []
    skipped_recent: list[str] = []
    skipped_current: list[str] = []
    now = now_timestamp if now_timestamp is not None else time.time()
    
    caps_dir = captures_dir(store)
    if not caps_dir.exists():
        return {
            "finalized": finalized,
            "skipped_recent": skipped_recent,
            "skipped_current": skipped_current,
        }
    
    for path in sorted(caps_dir.glob("*.jsonl")):
        # Extract session ID from filename (e.g., "session-id.jsonl" -> "session-id")
        session_id = path.stem
        
        if session_id == current_session_id:
            skipped_current.append(session_id)
            continue
        
        if is_stale_buffer(path, max_age_seconds=max_age_seconds, now_timestamp=now):
            try:
                finalize(
                    store, session_id, cwd=cwd,
                    generated_at=datetime.now(timezone.utc).isoformat(),
                )
                finalized.append(session_id)
            except Exception:
                # Never let a finalize failure break the scan
                pass
        else:
            skipped_recent.append(session_id)
    
    return {
        "finalized": finalized,
        "skipped_recent": skipped_recent,
        "skipped_current": skipped_current,
    }
```

Note: You'll need to import `datetime` and `timezone` at the top of capture.py if not already imported:

```python
from datetime import datetime, timezone
```

- [ ] **Step 8: Run all tests to verify they pass**

```bash
pytest tests/test_capture.py::test_finalize_all_except_skips_current_session tests/test_capture.py::test_finalize_all_except_finalizes_old_buffer tests/test_capture.py::test_finalize_all_except_skips_recent_buffers tests/test_capture.py::test_finalize_all_except_multiple_buffers tests/test_capture.py::test_finalize_all_except_empty_captures_dir tests/test_capture.py::test_finalize_all_except_returns_proposal_ids -xvs
```

Expected: All 6 PASS

- [ ] **Step 9: Commit**

```bash
git add src/vouch/capture.py tests/test_capture.py
git commit -m "feat(capture): add finalize_all_except() for old buffer cleanup

implement is_stale_buffer() to check file age, and finalize_all_except()
to bulk-finalize capture buffers older than a threshold. this enables
sessionstart cleanup of orphaned buffers from previous sessions.

includes 9 unit tests covering single/multiple buffers, age boundaries,
and current session exclusion."
```

---

## Task 2: Add `vouch capture finalize-all` CLI command

**Files:**
- Modify: `src/vouch/cli.py:800+` (after existing `finalize` command)
- Test: `tests/test_capture.py`

**Interfaces:**
- Consumes: `capture.finalize_all_except(store, session_id, max_age_seconds=...) -> dict`
- Produces: CLI command `vouch capture finalize-all [--session-id <ID>] [--max-age-seconds <N>]`

- [ ] **Step 1: Write the failing test for CLI command**

Add to `tests/test_capture.py`:

```python
def test_capture_finalize_all_cmd_with_old_buffers(tmp_path, monkeypatch):
    """CLI command should finalize old buffers and emit JSON."""
    store = _make_store(tmp_path)
    current_sess = "current"
    old_sess = "old-session"
    
    # Create old buffer
    old_path = capture_mod.buffer_path(store, old_sess)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    observations = [
        '{"ts": 1.0, "tool": "Read", "summary": "test1"}',
        '{"ts": 2.0, "tool": "Read", "summary": "test2"}',
        '{"ts": 3.0, "tool": "Read", "summary": "test3"}',
    ]
    old_path.write_text("\n".join(observations) + "\n")
    old_mtime = time.time() - 7200
    old_path.touch(times=(old_mtime, old_mtime))
    
    # Mock the store discovery and cwd
    monkeypatch.setenv("VOUCH_KB_ROOT", str(store.kb_root))
    
    runner = CliRunner()
    result = runner.invoke(cli, [
        "capture", "finalize-all",
        "--session-id", current_sess,
        "--max-age-seconds", "3600",
    ])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert old_sess in output["finalized"]
    assert current_sess in output["skipped_current"]


def test_capture_finalize_all_cmd_reads_session_from_env(tmp_path, monkeypatch):
    """CLI command should fall back to VOUCH_SESSION_ID env var."""
    store = _make_store(tmp_path)
    current_sess = "from-env"
    
    # Create current session buffer
    curr_path = capture_mod.buffer_path(store, current_sess)
    curr_path.parent.mkdir(parents=True, exist_ok=True)
    curr_path.write_text('{"ts": 1.0, "tool": "Read", "summary": "test"}\n')
    
    monkeypatch.setenv("VOUCH_KB_ROOT", str(store.kb_root))
    monkeypatch.setenv("VOUCH_SESSION_ID", current_sess)
    
    runner = CliRunner()
    result = runner.invoke(cli, ["capture", "finalize-all"])
    
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert current_sess in output["skipped_current"]


def test_capture_finalize_all_cmd_silent_on_no_kb(monkeypatch, tmp_path):
    """CLI command should silently succeed if KB not found."""
    monkeypatch.setenv("VOUCH_KB_ROOT", str(tmp_path / "nonexistent"))
    
    runner = CliRunner()
    result = runner.invoke(cli, [
        "capture", "finalize-all",
        "--session-id", "test",
    ])
    
    # Should exit 0, not fail
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_capture.py::test_capture_finalize_all_cmd_with_old_buffers -xvs
```

Expected: `FAILED ... no such option: --session-id` or similar

- [ ] **Step 3: Implement CLI command in cli.py**

Find the `@capture.command("finalize")` section (around line 740-760). After the `capture_finalize_cmd()` function and before the `@capture.command("banner")`, add:

```python
@capture.command("finalize-all")
@click.option("--session-id", default=None, help="Current session id (else env VOUCH_SESSION_ID).")
@click.option("--max-age-seconds", type=float, default=3600.0, help="Max age in seconds.")
def capture_finalize_all_cmd(session_id: str | None, max_age_seconds: float) -> None:
    """Finalize all capture buffers except current session (SessionStart cleanup)."""
    sid = session_id or os.environ.get("VOUCH_SESSION_ID") or ""
    if not sid:
        # No session ID provided; silently succeed
        _emit_json({"finalized": [], "skipped_recent": [], "skipped_current": []})
        return
    
    store = _capture_store()
    if store is None:
        # No KB; silently succeed
        _emit_json({"finalized": [], "skipped_recent": [], "skipped_current": []})
        return
    
    result = capture_mod.finalize_all_except(
        store, sid, max_age_seconds=max_age_seconds,
    )
    _emit_json(result)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_capture.py::test_capture_finalize_all_cmd_with_old_buffers tests/test_capture.py::test_capture_finalize_all_cmd_reads_session_from_env tests/test_capture.py::test_capture_finalize_all_cmd_silent_on_no_kb -xvs
```

Expected: All 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/vouch/cli.py tests/test_capture.py
git commit -m "feat(cli): add 'vouch capture finalize-all' command

new command finalizes all capture buffers except the given session,
if they are older than max_age_seconds (default 3600s). silently
succeeds if no KB found. used by sessionstart hook to clean up
orphaned buffers from previous sessions.

includes 3 unit tests covering option parsing, env fallback, and
graceful degradation."
```

---

## Task 3: Update adapter SessionStart hook

**Files:**
- Modify: `adapters/claude-code/.claude/settings.json:22-42`
- No test (hook configuration is integration-tested via the capture tests)

**Interfaces:**
- Consumes: CLI command `vouch capture finalize-all` (from Task 2)
- Produces: Updated hook configuration

- [ ] **Step 1: Modify the SessionStart hook**

Edit `adapters/claude-code/.claude/settings.json`. Change the `SessionStart` section from:

```json
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
      },
      {
        "type": "command",
        "command": "vouch recall || true"
      }
    ]
  }
]
```

To:

```json
"SessionStart": [
  {
    "matcher": "*",
    "hooks": [
      {
        "type": "command",
        "command": "vouch capture finalize-all || true"
      },
      {
        "type": "command",
        "command": "vouch status --json || true"
      },
      {
        "type": "command",
        "command": "vouch capture banner || true"
      },
      {
        "type": "command",
        "command": "vouch recall || true"
      }
    ]
  }
]
```

The key change: `finalize-all` is now the **first** hook, so old buffers are cleaned up before the banner and recall.

- [ ] **Step 2: Verify the JSON is valid**

```bash
python -c "import json; json.load(open('adapters/claude-code/.claude/settings.json'))"
```

Expected: No output (valid JSON)

- [ ] **Step 3: Commit**

```bash
git add adapters/claude-code/.claude/settings.json
git commit -m "feat(adapter): wire capture finalize-all into sessionstart hook

run 'vouch capture finalize-all' as the first sessionstart hook
to clean up orphaned buffers from previous sessions before
banner and recall commands. ensures old sessions are finalized
automatically on next session start."
```

---

## Task 4: Add WindowClose hook configuration (conditional)

**Files:**
- Modify: `adapters/claude-code/.claude/settings.json:52+`
- No test (integration-level, not unit-testable)

**Interfaces:**
- Consumes: CLI command `vouch capture finalize` (existing)
- Produces: WindowClose hook (conditional)

- [ ] **Step 1: Check if VS Code extension supports WindowClose event**

Check the Claude Code extension documentation or recent commits to see if `WindowClose` is a supported hook event. Look for:
- Extension release notes mentioning new hook types
- Tests or examples using `WindowClose`
- Comments in settings.json from recent commits

**For now, assume it's NOT supported yet.** If you find evidence that it IS supported, proceed to Step 2. Otherwise, skip to Step 3.

- [ ] **Step 2: (If WindowClose is supported) Add the hook**

Add this new hook section after the `PostToolUse` section in `adapters/claude-code/.claude/settings.json`:

```json
"WindowClose": [
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
```

Then commit:

```bash
git commit -m "feat(adapter): wire capture finalize into windowclose hook

finalize the current session's buffer when the vs code window closes,
creating a proposal immediately without waiting for next session start."
```

- [ ] **Step 3: (If WindowClose is NOT supported) Document the fallback**

Add a comment to the settings.json explaining the fallback behavior. Change the SessionStart comment to:

```json
"SessionStart": [
  {
    "comment": "finalize old buffers from previous sessions; current session will be finalized here too on next session start (fallback: windowclose event not yet supported)",
    "matcher": "*",
    ...
  }
]
```

Then update the adapter README to document this. See Task 5.

---

## Task 5: Update adapter README with behavior docs

**Files:**
- Modify: `adapters/claude-code/README.md`
- No test

**Interfaces:**
- Produces: User-facing documentation

- [ ] **Step 1: Add a "Session Capture" section to the README**

Find the README and add this section (after the "Installation" or "Features" section):

```markdown
## Session Capture & Auto-Proposal

When you work in a Claude Code session, vouch automatically captures your
tool use (file reads, edits, commands, etc.). When you close the session
window, vouch proposes the captured knowledge to the KB for review.

### How it works

1. **Capture**: Each tool call (Read, Edit, Bash, etc.) is logged to
   `.vouch/captures/<session-id>.jsonl` (gitignored).

2. **Cleanup on session start**: When you start a new session, any
   unfinalzed buffers from previous sessions (>1 hour old) are
   automatically finalized and proposed.

3. **Finalize on window close**: When the VS Code window closes, the
   current session is finalized and proposed.

### Configuration

Disable capture in `.vouch/config.yaml`:

```yaml
capture:
  enabled: false
```

Adjust the stale buffer age (default: 1 hour):

```yaml
capture:
  max_age_seconds: 7200  # finalize buffers >2 hours old
```

### Fallback behavior

If the "window close" event is not yet supported by your version of the
Claude Code extension, the current session will be finalized on the *next*
session start instead. The behavior is the same; proposals just appear in
the next session rather than immediately.

To upgrade or check your extension version, see [Claude Code releases](https://github.com/anthropics/claude-code-releases).
```

- [ ] **Step 2: Commit**

```bash
git add adapters/claude-code/README.md
git commit -m "docs(adapter): explain session capture auto-proposal behavior

add section describing how capture works, configuration options,
and the fallback behavior if windowclose event is not available."
```

---

## Task 6: Add integration test (smoke test)

**Files:**
- Modify: `tests/test_capture.py`
- (Optional make target in Makefile)

**Interfaces:**
- Consumes: All functions from Tasks 1-2
- Produces: End-to-end test

- [ ] **Step 1: Write the end-to-end test**

Add to `tests/test_capture.py`:

```python
def test_capture_e2e_sessionstart_cleanup_then_finalize(tmp_path):
    """End-to-end: old buffers cleaned up on sessionstart, current session on finalize."""
    store = _make_store(tmp_path)
    
    # Simulate a previous session that crashed/closed without finalize
    old_sess = "crashed-session"
    old_path = capture_mod.buffer_path(store, old_sess)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    observations = [
        '{"ts": 1.0, "tool": "Read", "summary": "test1"}',
        '{"ts": 2.0, "tool": "Read", "summary": "test2"}',
        '{"ts": 3.0, "tool": "Read", "summary": "test3"}',
    ]
    old_path.write_text("\n".join(observations) + "\n")
    old_mtime = time.time() - 7200  # 2 hours ago
    old_path.touch(times=(old_mtime, old_mtime))
    
    # Simulate a new session starting
    new_sess = "new-session"
    
    # 1. SessionStart cleanup (finalize old buffers)
    cleanup_result = capture_mod.finalize_all_except(
        store, new_sess, max_age_seconds=3600.0
    )
    assert old_sess in cleanup_result["finalized"]
    assert not old_path.exists()
    
    # Verify old session was proposed
    pending_before = store.list_proposals(ProposalStatus.PENDING)
    old_proposals = [p for p in pending_before if p.session_id == old_sess]
    assert len(old_proposals) == 1
    
    # 2. SessionEnd finalize (current session)
    new_path = capture_mod.buffer_path(store, new_sess)
    new_path.write_text("\n".join(observations) + "\n")
    
    finalize_result = capture_mod.finalize(store, new_sess)
    assert finalize_result["summary_proposal_id"] is not None
    assert not new_path.exists()
    
    # Verify new session was proposed
    pending_after = store.list_proposals(ProposalStatus.PENDING)
    new_proposals = [p for p in pending_after if p.session_id == new_sess]
    assert len(new_proposals) == 1
    
    # Total proposals: old + new
    assert len(pending_after) >= 2
```

- [ ] **Step 2: Run the test to verify it passes**

```bash
pytest tests/test_capture.py::test_capture_e2e_sessionstart_cleanup_then_finalize -xvs
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_capture.py
git commit -m "test(capture): add e2e test for sessionstart cleanup + finalize flow

verify that old buffers are cleaned up on new session start
and current session is finalized on window close (or manual finalize),
resulting in two separate proposals."
```

---

## Task 7: Run full test suite and verify no regressions

**Files:** (no changes)

- [ ] **Step 1: Run all capture tests**

```bash
source .venv/bin/activate
pytest tests/test_capture.py -v
```

Expected: All tests PASS (>15 tests now)

- [ ] **Step 2: Run full test suite (ignore embeddings)**

```bash
pytest tests/ -q --ignore=tests/embeddings
```

Expected: No new failures, all existing tests still pass

- [ ] **Step 3: Run mypy type check**

```bash
python -m mypy src
```

Expected: No new errors, all files type-check

- [ ] **Step 4: Run ruff lint**

```bash
python -m ruff check src tests
```

Expected: No new issues, all code follows style

- [ ] **Step 5: Run make check (convenience wrapper)**

```bash
make check
```

Expected: All checks green

---

## Task 8: Commit spec and plan files

**Files:**
- Already created: `docs/superpowers/specs/2026-07-01-vscode-session-autoproposal-design.md`
- Already created: `docs/superpowers/plans/2026-07-01-vscode-session-autoproposal.md`

- [ ] **Step 1: Add design doc and plan to git**

```bash
git add docs/superpowers/specs/2026-07-01-vscode-session-autoproposal-design.md
git add docs/superpowers/plans/2026-07-01-vscode-session-autoproposal.md
git commit -m "docs(superpowers): add design and plan for vscode session auto-proposal

design: automatic finalization of old buffers on sessionstart,
and current session on window close.

plan: 8-task breakdown covering capture.py functions, cli command,
adapter hooks, comprehensive unit tests, integration test, and
documentation."
```

---

## Summary

| Task | Deliverable | Tests |
|------|-------------|-------|
| 1 | `is_stale_buffer()`, `finalize_all_except()` in capture.py | 9 unit tests |
| 2 | `vouch capture finalize-all` CLI command | 3 unit tests |
| 3 | Updated SessionStart hook in adapter settings | 0 (config) |
| 4 | WindowClose hook (conditional) | 0 (config) |
| 5 | README docs on behavior | 0 (docs) |
| 6 | E2E integration test | 1 test |
| 7 | Full regression testing | 0 (validation) |
| 8 | Commit design & plan | 0 (docs) |

**Total new tests: 13+ unit tests + 1 integration test**

---

## Execution

**Plan complete and saved to `docs/superpowers/plans/2026-07-01-vscode-session-autoproposal.md`.**

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (or 2-3 related tasks), review between tasks, fast iteration with checkpoints

2. **Inline Execution** — Execute tasks in this session sequentially with checkpoints for review

**Which approach would you prefer?**
