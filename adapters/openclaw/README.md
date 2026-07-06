# openclaw adapter

Two different things both go by "the OpenClaw integration":

1. **Loading vouch into an OpenClaw deployment.** That's the repo-root
   [`openclaw.plugin.json`](../../openclaw.plugin.json) manifest plus
   [`package.json`](../../package.json) (the loader-facing
   `openclaw.extensions` pointer at
   [`vouch-context-engine.mjs`](./vouch-context-engine.mjs)) —
   `openclaw plugins install --link <repo>` registers the context engine
   (auto-bound to `plugins.slots.contextEngine`) and publishes the four
   skills under [`skills/`](./skills/) as slash commands. The kb.* MCP
   server is deployment config: `openclaw mcp add vouch -- vouch serve`.
   See the README section "Running vouch as an OpenClaw plugin".
2. **Enabling vouch in one OpenClaw-managed project.** That's this
   adapter, run with `vouch install-mcp openclaw --path <project>`.

The writer drops:

- `.openclaw/plugins.json` — declares the vouch plugin enabled for this
  project (T1).
- `AGENTS.md` — a fenced snippet pointing at the plugin manifest and
  summarizing the review-gate contract (T2).
- `.claude/commands/vouch-*.md` — the same four slash commands the
  claude-code adapter ships, referenced in place rather than duplicated
  (T3).
- `.openclaw/policy.json` — the trust boundary as project-local policy:
  review-gated writes, audit-logged lifecycle ops, confined filesystem
  access for remote callers (T4).

```sh
vouch install-mcp openclaw --path .
```

Re-running is idempotent: existing files are left alone, and `AGENTS.md`
gets the vouch block appended once inside a fence so reruns don't
duplicate it.
