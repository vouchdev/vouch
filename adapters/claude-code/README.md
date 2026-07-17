# Claude Code adapter

Wires `vouch serve` (MCP, stdio) into [Claude Code][cc].

[cc]: https://claude.com/claude-code

## 1. Install vouch

```bash
pipx install vouch-kb
# or, from a checkout: pip install -e '/path/to/vouch[dev]'
```

Make sure `vouch` is on the `PATH` Claude Code will see.

The one-command path — `vouch install-mcp claude-code` from your project
root — does everything below in one go, initialises the `.vouch/` KB
first when the project doesn't have one yet (`vouch init` also does that
on its own; `--no-init` skips it), **and registers vouch in
`~/.claude.json` so the VS Code extension loads it without a manual
approval it never prompts for** (see step 2). The rest of this file is
the manual equivalent.

## 2. Drop the MCP server into your project

Add `.mcp.json` at the root of your project (the same directory that
contains `.vouch/` — created by `vouch init` if you're wiring by hand):

```json
{
  "mcpServers": {
    "vouch": {
      "command": "vouch",
      "args": ["serve"],
      "env": {
        "VOUCH_AGENT": "claude-code"
      }
    }
  }
}
```

A `.mcp.json` server is *project*-scope: the terminal CLI prompts once to
approve it, but **the VS Code extension never surfaces that prompt**, so on
its own it sits at "pending approval" and the `kb_*` tools never appear (the
hooks, which need no approval, keep working — easy to misread as connected).
To load it in the extension, register the same server at *local* scope, which
is trusted on sight:

```bash
claude mcp add vouch --env VOUCH_AGENT=claude-code -- vouch serve
```

(the one-command `install-mcp` above does this for you). Then reload the
editor window. Confirm with `claude mcp list` — `vouch … ✔ Connected`.

## 3. Teach Claude about the gate

Add a paragraph to your `CLAUDE.md` (or the project's `AGENTS.md`):

> This repo uses **vouch** for durable knowledge. To remember
> something across sessions, call `kb_propose_claim` (or
> `kb_propose_page`/`kb_propose_entity`/`kb_propose_relation`) with at
> least one citation — every claim needs evidence. Do not call any
> `kb_*` method that bypasses proposals; the gate is the whole point.
> Read with `kb_search` and `kb_context`. The human reviewer runs
> `vouch approve` from the terminal.

That's it. Once Claude knows the gate exists, it will use it.

## 4. Verify

In a fresh session, ask Claude:

> What knowledge-base tools do you have?

It should enumerate `kb_search`, `kb_propose_claim`, etc. If not, check
`claude mcp list` first: `vouch … ⏸ Pending approval` means the local-scope
registration from step 2 hasn't happened (`✔ Connected` once it has), and a
running editor window needs a reload to pick it up. For anything else, run
`claude --debug-mcp` to see why the server isn't loading.

## Session Capture & Auto-Proposal

When you work in a Claude Code session, vouch automatically captures your
tool use (file reads, edits, commands, etc.). When you close the session
window, vouch proposes the captured knowledge to the KB for review.

### How it works

1. **Capture**: Each tool call (Read, Edit, Bash, etc.) is logged to
   `.vouch/captures/<session-id>.jsonl` (gitignored).

2. **Cleanup on session start**: When you start a new session, any
   unfinalized buffers from previous sessions (>1 hour old) are
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

## Notes

- `VOUCH_AGENT=claude-code` shows up as the actor in `audit.log.jsonl`
  and as `proposed_by` on every proposal. Use a different value if
  you run multiple Claude Code seats against the same KB and want to
  tell them apart.
- The server respects `cwd` — it discovers `.vouch/` by walking up
  from the directory Claude Code launched it in.
- If you want Claude to also know about lifecycle methods
  (`kb_supersede`, `kb_contradict`, …) without you asking each time,
  add: "When you find a stale claim, supersede it rather than
  proposing a contradicting one."
- Only a core set of `kb_*` tools is visible by default (`mcp.tool_profile:
  minimal` in `.vouch/config.yaml`, or the `VOUCH_TOOL_PROFILE` env var).
  Set it to `standard` or `full` to expose lifecycle/admin tools.
