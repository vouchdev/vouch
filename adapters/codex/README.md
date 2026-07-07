# Codex CLI adapter

Wires `vouch serve` into [OpenAI's Codex CLI][codex] as an MCP server.

[codex]: https://github.com/openai/codex

## Quick start

```bash
vouch install-mcp codex
```

This writes a project-local `.codex/config.toml` (T1) and, at T2,
appends a fenced snippet to `AGENTS.md` so Codex knows how to use the
KB tools. Restart any running Codex session.

## Tiers

| Tier | What it adds | File |
|---|---|---|
| T1 | MCP wire — `vouch serve` as an MCP server | `.codex/config.toml` |
| T2 | AGENTS.md snippet — standing instructions for recall, propose-don't-write, and the human review gate | `AGENTS.md` (fenced) |

Install a specific tier:

```bash
vouch install-mcp codex --tier T1   # MCP config only
vouch install-mcp codex --tier T2   # MCP config + AGENTS.md snippet
```

Re-running is idempotent: existing files are left alone, and an
existing `AGENTS.md` fence is preserved without duplicating the block.

## Manual setup

If you prefer to edit the config by hand, add a `vouch` entry to
`~/.codex/config.toml`:

```toml
[mcp_servers.vouch]
command = "vouch"
args = ["serve"]

[mcp_servers.vouch.env]
VOUCH_AGENT = "codex"
```

Or use the project-local form (what `vouch install-mcp codex --tier T1`
writes) at `<project>/.codex/config.toml`. The project-local form is
preferred because it doesn't touch home-directory state.

## Notes

- Codex respects MCP tool naming verbatim, so the tools appear as
  `kb_search`, `kb_propose_claim`, etc.
- If Codex's tool surface is large and you only want vouch-related
  tools surfaced, use Codex's tool-allow-list in
  `~/.codex/config.toml`:

```toml
[allowed_tools]
vouch = "*"
```

- The `vouch` command must be on the `PATH` that Codex inherits.
  Use `which vouch` to confirm and put the absolute path into
  `command` if Codex is launched from a context that lacks your shell
  env (e.g. via a GUI launcher).
