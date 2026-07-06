# Codex CLI adapter

Wires `vouch serve` into [OpenAI's Codex CLI][codex] as an MCP server.

[codex]: https://github.com/openai/codex

## Setup

```bash
vouch install-mcp codex
```

This writes the `vouch` MCP entry into the *project-local*
`<project>/.codex/config.toml` (deep-merged into any existing one, so
your other servers and settings are preserved) — never into
`~/.codex/config.toml`, per the project-scoped install rule. Codex
loads project-local `.codex/` config for trusted projects, so trust
the project when codex asks.

Prefer a user-global setup, or no installer? Add the entry to
`~/.codex/config.toml` (or `<project>/.codex/config.toml`) by hand:

```toml
[mcp_servers.vouch]
command = "vouch"
args = ["serve"]

[mcp_servers.vouch.env]
VOUCH_AGENT = "codex"
```

Restart any running `codex` session.

## Skills (T3)

`vouch install-mcp codex --tier T3` also drops the vouch guided flows
(`vouch-recall`, `vouch-status`, `vouch-resolve-issue`,
`vouch-propose-from-pr`, plus the company-brain set) into the
project-local `.codex/skills/` directory. Codex discovers skills from
`<project>/.codex/skills/` in trusted projects, so they surface in the
session automatically — ask for a skill by name (e.g. "use the
vouch-recall skill for X") or let codex pick them up from context.

Why skills and not custom prompts: codex loads custom prompts only
from `~/.codex/prompts/` (user-global) and has deprecated them in
favour of skills. A project-scoped `vouch install-mcp` never touches
home-directory state, so skills are the surface vouch ships. If you
prefer slash-style prompts anyway, copy the installed
`.codex/skills/*/SKILL.md` bodies into `~/.codex/prompts/<name>.md`
yourself.

The skill bodies are identical to the claude-code slash commands
(enforced by a sync test), so the flows behave the same on every host.

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
