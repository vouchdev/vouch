# Codex CLI adapter

Wires `vouch serve` into [OpenAI's Codex CLI][codex] as an MCP server.

[codex]: https://github.com/openai/codex

## Setup

Codex reads MCP server config from `~/.codex/config.toml`. Add a
`vouch` entry:

```toml
[mcp_servers.vouch]
command = "vouch"
args = ["serve"]

[mcp_servers.vouch.env]
VOUCH_AGENT = "codex"
```

Restart any running `codex` session.

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
