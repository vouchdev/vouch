# Codex CLI adapter

Wires vouch into [OpenAI's Codex CLI][codex]: the MCP server, standing
`AGENTS.md` instructions, the vouch guided flows as skills, and
automatic session capture.

[codex]: https://github.com/openai/codex

## Install

```bash
vouch install-mcp codex              # everything (T1–T4)
vouch install-mcp codex --tier T1    # just the MCP wire
```

Everything lands in the *project*, never in `~/.codex` — the same
scope rule every adapter follows. Codex loads project-local `.codex/`
config for trusted projects, so trust the project when codex asks.

What each tier adds (tiers stack):

| Tier | File | What it does |
|---|---|---|
| T1 | `.codex/config.toml` | registers the `vouch` MCP server (`kb_search`, `kb_propose_claim`, …) with `VOUCH_AGENT=codex` for audit attribution |
| T2 | `AGENTS.md` | fenced snippet with the standing rules: recall first, all writes via proposals, review stays human |
| T3 | `.codex/skills/vouch-*/SKILL.md` | the nine vouch guided flows as project-local skills |
| T4 | `.codex/hooks.json` | `Stop` hook that auto-captures each session into a pending, review-gated summary |

The install is idempotent and merge-safe: an existing
`.codex/config.toml` or `.codex/hooks.json` is deep-merged into (your
entries always win on conflict), an existing `AGENTS.md` gets the
snippet appended inside fence markers, and re-runs are a flat no-op.

## Skills (T3)

Codex discovers skills from `<project>/.codex/skills/` in trusted
projects, so the flows (`vouch-recall`, `vouch-status`,
`vouch-resolve-issue`, `vouch-propose-from-pr`, plus the company-brain
set) surface in the session automatically — ask for one by name or let
codex pick them up from context.

Why skills and not custom prompts: codex loads custom prompts only
from `~/.codex/prompts/` (user-global) and has deprecated them in
favour of skills. A project-scoped `vouch install-mcp` never touches
home-directory state, so skills are the surface vouch ships. If you
prefer slash-style prompts anyway, copy the installed
`.codex/skills/*/SKILL.md` bodies into `~/.codex/prompts/<name>.md`
yourself.

The skill bodies are identical to the claude-code slash commands
(enforced by a sync test), so the flows behave the same on every host.

## Automatic session capture (T4)

`.codex/hooks.json` registers a `Stop` hook that runs `vouch capture
ingest-codex --hook` when a turn completes. The handler reads the hook
payload, resolves the session's rollout file, and rolls it into ONE
pending session-summary proposal — the same review-gated summary a
claude-code session produces. Because `Stop` fires per turn, re-ingest
is idempotent: an unchanged session is a no-op, a session that grew
refreshes its pending proposal in place, and a proposal you've already
reviewed is never resurrected.

Failure semantics match `capture observe`: the `--hook` mode exits 0
no matter what, so a capture problem can never break your codex turn.
Nothing is auto-approved — review with `vouch review`.

Past sessions can be ingested by hand too:

```bash
vouch capture ingest-codex --latest        # newest rollout for this project
vouch capture ingest-codex <rollout.jsonl> # a specific one
```

Why not codex's `notify` setting: codex honours `notify` only in
user-global config (`~/.codex/config.toml`), which a project-scoped
install never touches. If you prefer notify anyway, point it at a
wrapper that calls `vouch capture ingest-codex --hook` yourself.

## Manual fallback

No installer, or a user-global setup on purpose? Add the entry to
`~/.codex/config.toml` (or `<project>/.codex/config.toml`) by hand:

```toml
[mcp_servers.vouch]
command = "vouch"
args = ["serve"]

[mcp_servers.vouch.env]
VOUCH_AGENT = "codex"
```

Restart any running `codex` session, then confirm with
`codex mcp list`.

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
