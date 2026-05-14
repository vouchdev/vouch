# Continue.dev adapter

Wires `vouch serve` into [Continue][continue] as an MCP server.

[continue]: https://continue.dev

## Setup

Continue reads its config from `~/.continue/config.json` (legacy) or
`~/.continue/config.yaml` (current). For the YAML form:

```yaml
mcpServers:
  - name: vouch
    command: vouch
    args: ["serve"]
    env:
      VOUCH_AGENT: continue
```

Restart Continue (`Cmd-Shift-P → Continue: Reload`).

## Notes

- Continue may not surface MCP tools by default in chat — check the
  Tools toggle in the chat panel.
- Continue's prompt templates may interfere with tool calling on
  smaller models. If `kb_propose_claim` keeps producing arguments
  that don't validate, switch to a model with stronger tool-use
  support.
