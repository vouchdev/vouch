# Claude Code adapter

Wires `vouch serve` (MCP, stdio) into [Claude Code][cc].

[cc]: https://claude.com/claude-code

## 1. Install vouch

```bash
pip install -e '/path/to/vouch[dev]'
# or from PyPI (package vouch-kb): pipx install vouch-kb
```

Make sure `vouch` is on the `PATH` Claude Code will see.

## 2. Drop the MCP server into your project

Add `.mcp.json` at the root of your project (the same directory that
contains `.vouch/`):

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

Claude Code will pick it up the next time you open the project.

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

It should enumerate `kb_search`, `kb_propose_claim`, etc. If not, run
`claude --debug-mcp` to see why the server isn't loading.

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
