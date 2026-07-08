# adapters/

Drop-in glue for connecting vouch to specific LLM hosts.

Each subdirectory is a small, self-contained example of wiring `vouch
serve` into one host. They are *templates*, not packages — copy the
file you need into your project and edit it.

| Adapter | Host | Files |
|---|---|---|
| [claude-code/](claude-code/) | Anthropic's Claude Code CLI | `.mcp.json` snippet, `CLAUDE.md` excerpt |
| [cursor/](cursor/) | Cursor IDE | `mcp.json` snippet |
| [codex/](codex/) | OpenAI's Codex CLI | tiered install: `.codex/config.toml` merge, `AGENTS.md` excerpt, skills, capture hook |
| [continue/](continue/) | Continue.dev | `config.json` snippet |
| [openclaw/](openclaw/) | OpenClaw plugin host | `.openclaw/plugins.json`, `AGENTS.md` excerpt |
| [generic-mcp/](generic-mcp/) | Any MCP-speaking host | annotated reference |
| [jsonl-shell/](jsonl-shell/) | bash scripts via the JSONL transport | example pipeline |

## What an adapter is responsible for

Everything host-specific. That includes:

1. **How to launch `vouch serve`** with the right transport and env.
2. **What identity to give the agent.** Set `VOUCH_AGENT` distinctly
   per host so the audit log can attribute writes.
3. **Where to put the host's configuration file.** Each host has its
   own preferred location.
4. **Tool-name mapping.** MCP hosts see methods as `kb_search`,
   `kb_propose_claim` etc. — see [spec/transports.md](../spec/transports.md#tool-naming).

## What an adapter is *not* responsible for

- The KB's contents — those live in `.vouch/` regardless of host.
- Approval workflow — that's `vouch approve` from your shell. No
  adapter routes approval through the agent.
- Method semantics — the `kb.*` surface behaves identically in every
  host.

## Adding an adapter

Open a PR. New adapter directories should contain:

- A short `README.md` explaining where the snippet goes and what
  changes to make.
- The minimal config snippet itself (one file).
- Optional: an `agent-prompt.md` with phrasing tips for that host's
  conventions (e.g. "Claude Code reads CLAUDE.md; here's a paragraph
  to drop in").

No code. Adapters are configuration, not packages — anything that
needs Python belongs in the core.
