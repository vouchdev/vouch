# Give your coding agent a reviewed memory

Coding agents got very good at code. They're still amnesiac about everything
else — every time the context window closes, the agent forgets why the project
chose JWTs, which approach was already tried and rejected, and what the team
agreed last month. By the end of this tutorial your agent will read the
project's reviewed knowledge before it answers, and propose new claims as it
works — with you on the approve button.

- **Time:** about 10 minutes
- **You'll need:** a vouch KB (do the
  [first tutorial](first-knowledge-base.md) if you don't have one) and an
  MCP-capable agent — Claude Code, Codex, or Cursor

## 1. Wire it in one command

The fastest path is the agent's own MCP registration. `vouch serve` is a plain
stdio MCP server, so Claude Code (or Codex) can register it directly — no
vouch-specific installer needed:

```bash
claude mcp add vouch -- vouch serve    # or: codex mcp add vouch -- vouch serve
```

```
Added stdio MCP server vouch with command: vouch serve to project config
```

Confirm it connected:

```bash
claude mcp list
```

```
vouch: vouch serve - ✓ Connected
```

That's enough to give the agent vouch's `kb_*` tools. Add
`-e VOUCH_AGENT=claude-code` to the `add` command if you want the agent's
proposals attributed to it rather than your shell user.

The rest of this tutorial uses `vouch install-mcp` instead, which does the same
wiring **and** drops in the brain-first protocol, slash commands, and hooks. See
what's supported:

```bash
vouch install-mcp --list
```

```
Available MCP host adapters:
  - claude-code
  - claude-desktop
  - cline
  - codex
  - continue
  - cursor
  - openclaw
  - windsurf
  - zed
```

Install for your host (Claude Code shown; swap in `codex`, `cursor`, …):

```bash
vouch install-mcp claude-code
```

```
  + .mcp.json
  + CLAUDE.md
Done — 2 written, 0 appended, 0 skipped under /your/project
```

It's idempotent — re-running skips anything already in place. Tiers control how
much it writes:

| Tier | Writes |
|---|---|
| `T1` | the MCP wire only (`.mcp.json`) |
| `T2` | `+ CLAUDE.md` / `AGENTS.md` — the brain-first protocol |
| `T3` | `+` slash commands |
| `T4` (default) | `+` host hooks / settings |

```bash
vouch install-mcp claude-code --tier T2   # stop at the protocol, skip hooks
```

## 2. What it wrote

The `.mcp.json` points the host at vouch's MCP server:

```json
{
  "mcpServers": {
    "vouch": {
      "command": "vouch",
      "args": ["serve"],
      "env": { "VOUCH_AGENT": "claude-code" }
    }
  }
}
```

`vouch serve` is the MCP server over stdio; `VOUCH_AGENT` is the identity
recorded as `proposed_by` and as the actor on every audit event — so "which
agent claimed what" is always answerable, and (per the
[review gate](first-knowledge-base.md#5-review-it--the-gate)) the agent can't
approve its own proposals.

The `CLAUDE.md` adds the **brain-first protocol** — the habits that make the
memory worth having:

1. **Search before answering.** Call `kb_search` / `kb_context` before
   reasoning from scratch, so you reuse the agreed answer instead of re-deriving
   it.
2. **Capture decisions as you make them.** When the work settles a question,
   file it with `kb_propose_claim`, citing the evidence.
3. **Cite what you recall.** Answers built from the KB carry their claim ids, so
   a human can trace any fact back to its source.

## 3. Restart and confirm the agent can read

Restart the agent so it picks up `.mcp.json`. vouch's tools are now live:
`kb_search`, `kb_context`, `kb_read_claim`, `kb_propose_claim`, and the rest of
the `kb_*` surface (run `vouch capabilities` for the full list).

Ask it something the KB knows — "how does auth work in this project?" — and it
will call `kb_search` / `kb_context` and answer from the reviewed claim instead
of guessing.

## 4. Ambient capture — the agent proposes

Now the half that makes the memory grow. As the agent works and settles a
decision, it files a proposal. That's the agent calling `kb_propose_claim`; from
the CLI the same thing looks like:

```bash
VOUCH_AGENT=claude-code vouch propose-claim \
  --text "Refresh tokens rotate on every use; reuse is treated as theft." \
  --source <source-id> \
  --type decision \
  --confidence 0.9
```

The proposal lands in the pending queue, attributed to the agent — **not** in
the KB yet:

```bash
vouch pending
```

```
• 20260630-074020-4037420c  [claim]  by claude-code
    Refresh tokens rotate on every use; reuse is treated as theft.
```

## 5. You review — the gate, again

The agent proposed; a human approves. Because the proposer is `claude-code` and
you're not, the gate is satisfied by your approving:

```bash
vouch show 20260630-074020-4037420c        # inspect what the agent claimed
vouch approve 20260630-074020-4037420c --reason "confirmed against the auth ADR"
# or, if it's wrong:
vouch reject 20260630-074020-4037420c --reason "we don't rotate on every use"
```

Only after approval does the claim become durable and show up in the next
session's `kb_search`. A wrong fact an agent hallucinated never silently
propagates — it sits in `pending` until a human accepts or rejects it. Both
decisions are recorded in the audit log.

## 6. Scope work into a session (optional)

For a longer task, open a session so the agent's proposals are grouped and you
can approve the batch at once:

```bash
vouch session start --task "harden auth"
```

```
sess-20260630-073951-ad7971
```

The agent proposes against that session as it works; when you're happy with the
whole run:

```bash
vouch crystallize sess-20260630-073951-ad7971   # approve every pending proposal in the session
vouch session end sess-20260630-073951-ad7971
```

## The four habits that make it worth it

- **Brain-first lookup** — the agent retrieves the agreed answer instead of
  asking you or re-deriving it.
- **Ambient capture** — decisions get logged as proposals while the work
  happens, not in a doc nobody updates.
- **One shared brain** — Claude Code, Codex, and Cursor read and write the
  *same* `.vouch/`, so knowledge captured in one is instantly there in the
  others.
- **Reviewed before trusted** — every recalled fact passed the gate, which is
  what makes an agent memory safe to rely on.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Agent doesn't see the `kb_*` tools | Host didn't reload `.mcp.json`. | Fully restart the agent / reload the window. |
| `kb_search` returns nothing | KB is empty or the query misses. | Confirm with `vouch status`; seed a claim via the [first tutorial](first-knowledge-base.md). |
| Agent's proposal won't approve as itself | Self-approval is blocked by design. | Approve from a human identity (a different `VOUCH_AGENT`). |
| Wrong host config written | Wrong adapter name. | `vouch install-mcp --list`, then re-run with the right host. |

## Next steps

- [Share a knowledge base across machines and teammates](share-a-knowledge-base.md)
  — give every agent on the team the same reviewed brain.
- [Edit your KB as markdown in Obsidian](edit-in-obsidian.md) — review and
  extend the KB outside the terminal.
- [Per-host adapter details](../../adapters/) — exactly what each host's
  `install-mcp` writes.
