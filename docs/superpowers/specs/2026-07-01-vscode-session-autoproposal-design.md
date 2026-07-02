# VS Code Session Auto-Proposal Design

**Date:** 2026-07-01  
**Status:** Design phase  
**Goal:** When a Claude Code session closes in VS Code, automatically finalize and propose its captured knowledge to vouch, without user action.

## Problem

Today, vouch captures observations from VS Code Claude Code sessions into `.vouch/captures/<session-id>.jsonl`. The `vouch capture finalize` command works correctly and creates proposals. However:

1. The `SessionEnd` hook (configured in `adapters/claude-code/.claude/settings.json`) is **not firing** when the user closes the session window
2. Users must manually run `vouch capture finalize` to turn captured observations into a proposal
3. Sessions that crash or are force-closed leave orphaned capture buffers with no way to finalize them

## Solution Overview

Implement automatic finalization through two mechanisms:

### **Mechanism 1: SessionStart cleanup (old sessions)**
When a new Claude Code session starts, scan `.vouch/captures/` and finalize any buffers that don't belong to the current session. This catches:
- Previous sessions that closed but weren't finalized
- Crashed sessions with stale buffers
- Sessions from other projects (optional: filter by cwd)

### **Mechanism 2: Window close handler (current session)**
When the VS Code window closes or the extension unloads, finalize the current session's buffer.

**Implementation strategy:** Use Approach 1 (preferred) if the extension exposes a close event; fall back to Approach 2 (lazy finalization at next SessionStart) if not.

## Architecture Changes

### Files to modify/create

1. **`src/vouch/capture.py`**
   - Add `finalize_all_except(session_id)` — finalize buffers for sessions other than the one given
   - Add `is_stale_buffer(path, age_seconds)` — check if a buffer is older than a threshold (e.g., 1 hour)

2. **`src/vouch/cli.py`**
   - Add `vouch capture finalize-all` command — public entry point for SessionStart hook
   - Modify existing `capture_finalize_cmd` to handle the current session explicitly

3. **`adapters/claude-code/.claude/settings.json`**
   - Update `SessionStart` hook to run `vouch capture finalize-all` *before* the banner/recall (to clean up old sessions first)
   - Try to wire a proper close event if the extension supports it; document fallback behavior

4. **`tests/test_capture.py`**
   - Add tests for `finalize_all_except()` with multiple buffers
   - Add tests for stale buffer detection
   - Add end-to-end test: create multiple buffers, start new session, verify old ones finalize

## Behavior Specification

### SessionStart: clean up old buffers

**Trigger:** `SessionStart` hook (fires when new session begins)

**Command:** `vouch capture finalize-all [--session-id <id>] [--max-age <seconds>]`

**Behavior:**
1. Read `.vouch/captures/` for all `.jsonl` files
2. For each buffer:
   - If the session ID matches the current session, skip it
   - If the buffer is older than `--max-age` (default: 3600s / 1 hour), finalize it
   - Otherwise, skip it (not old enough)
3. Finalize each stale buffer into a proposal
4. Log summary (e.g., "finalized 2 old buffers")

**Exit behavior:** Never fail the session start. Silently succeed if no buffers to finalize, or if finalization encounters errors.

### Window close: finalize current session

**Trigger:** Window close / extension unload (if available); fallback to lazy finalization

**Desired behavior:**
- Hook fires when VS Code window closes
- Command: `vouch capture finalize --session-id <id>` (existing command)
- Result: Proposal is created and appears in vouch immediately

**Fallback (if close hook unavailable):**
- Current session buffer is finalized on *next* SessionStart (caught by cleanup mechanism)
- Proposal appears in the next session, not immediately
- Document this limitation in the adapter README

## API Design

### New: `capture.finalize_all_except()`

```python
def finalize_all_except(
    store: KBStore,
    current_session_id: str,
    *,
    max_age_seconds: float = 3600.0,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """
    Finalize all buffers except current_session_id, if they're older than max_age.
    
    Returns:
      {
        "finalized": [id1, id2, ...],
        "skipped_recent": [id3, id4, ...],
        "skipped_current": [id5],
      }
    """
```

### Modified: `vouch capture finalize-all` command

```
vouch capture finalize-all [--session-id <ID>] [--max-age-seconds <N>]

Finalize all capture buffers except the given session (current session).
Falls back to reading from env: VOUCH_SESSION_ID, VOUCH_SESSION_MAX_AGE.
```

### Error handling

- If `_capture_store()` returns None (KB not initialized), silently succeed
- If finalize fails for one buffer, continue with the rest (don't cascade)
- Never raise exceptions that break the SessionStart hook

## Hook Configuration (adapter settings)

```json
{
  "hooks": {
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
    ],
    "PostToolUse": [ ... ],
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
  }
}
```

If `WindowClose` event is not available in the extension, this section is removed and a note is added to the README documenting the fallback behavior.

## Testing Strategy

1. **Unit tests** (`tests/test_capture.py`):
   - `test_finalize_all_except_with_multiple_buffers()`
   - `test_finalize_all_except_skips_current_session()`
   - `test_finalize_all_except_skips_recent_buffers()`
   - `test_finalize_all_except_removes_stale_buffers()`

2. **CLI tests**:
   - `test_capture_finalize_all_command()`

3. **End-to-end smoke test** (make target):
   - Create mock session with observations
   - Simulate SessionStart
   - Verify old buffers are finalized
   - Verify pending count increases

## Scope & Constraints

**In scope:**
- SessionStart cleanup of old buffers
- Public `finalize-all` command
- Tests
- Adapter hook wiring (conditional on extension support)

**Out of scope:**
- Changing the capture observation mechanism
- Storage format changes
- Cross-project buffer isolation (all buffers in one KB)
- Compression or archiving of old proposals

**Constraints:**
- Never break SessionStart (all errors are caught)
- Backward compatible with existing `finalize` command
- Works with current KB structure (no schema changes)
- Max age defaults to 1 hour (configurable)

## Success Criteria

1. ✅ Old capture buffers from previous sessions are finalized when a new session starts
2. ✅ Current session buffer is finalized when the window closes (if hook available)
3. ✅ Proposals appear in vouch `pending` list without user action
4. ✅ All tests pass, no regressions
5. ✅ Adapter settings are updated with hook configuration
6. ✅ README documents the behavior and fallback

## Open Questions

1. Does the VS Code Claude Code extension fire a `WindowClose` event? If not, document the fallback clearly.
2. Should max-age be configurable per project in `config.yaml`? (Defer to future if not critical)
3. Should we filter buffers by `cwd` to avoid finalizing sessions from other projects? (Future enhancement)

---

**Next steps:**
1. User reviews this spec
2. Implement `finalize_all_except()` and CLI command
3. Update adapter hooks
4. Write tests
5. Test end-to-end
