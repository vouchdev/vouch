# Cursor adapter

Wires `vouch serve` into [Cursor][cursor] as an MCP server.

[cursor]: https://cursor.com

## Setup

Cursor reads MCP servers from `~/.cursor/mcp.json` (global) or
`.cursor/mcp.json` (per-project). Use the per-project file so the
server only loads when you're working in this repo.

```json
{
  "mcpServers": {
    "vouch": {
      "command": "vouch",
      "args": ["serve"],
      "env": {
        "VOUCH_AGENT": "cursor"
      }
    }
  }
}
```

Restart Cursor. The `vouch` tools should appear in the Composer tool
list.

## .cursorrules excerpt

Add to `.cursorrules`:

> When proposing knowledge for the project KB, use the `vouch` MCP
> tools. Every claim needs a citation. Do not skip the proposal step
> — durable writes require human approval via `vouch approve`.

## Notes

- Cursor sometimes caches MCP server lists. If `vouch` doesn't show
  up, try `Cmd-Shift-P → Reload Window`.
- Cursor's Composer can call tools in parallel. The vouch server
  handles concurrent requests fine, but be aware that two proposals
  filed in the same Composer turn will get different ids.
