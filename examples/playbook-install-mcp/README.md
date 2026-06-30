# Wire vouch into a host (install-mcp)

How an operator installs vouch into Claude Code, Cursor, or another host, with
stacked adoption tiers (T1 MCP wire .. T4 host hooks), then confirms the host
can discover the kb.* surface the wire points at.

This is a **playbook** example. A real install targets *your own* project tree,
and the higher tiers (T2..T4) write `CLAUDE.md` / `AGENTS.md`, slash commands,
and host settings into that tree. `run.sh` runs only the safe, read-only legs:
`install-mcp --list`, a **T1** install into a throwaway temp project, and the
two discovery calls a host makes first. No network, no spawned agent.

## Run it:

```bash
VOUCH=/path/to/vouch ./examples/playbook-install-mcp/run.sh
```

It builds a throwaway KB in `$(mktemp -d)`, runs the read-only install legs, and
cleans up. Override the binary with `VOUCH=...`; default is `vouch` on `PATH`.

## The tier ladder

`vouch install-mcp <host> --tier T1|T2|T3|T4` is idempotent and the tiers
**stack** — T1 ⊂ T2 ⊂ T3 ⊂ T4. Pick the lowest tier that does the job; you can
re-run at a higher tier later and only the new files get written.

| Tier | Adds | Why you'd stop here |
|------|------|---------------------|
| **T1** | the MCP wire only (`.mcp.json` for claude-code) | you just want the host to *reach* vouch; you manage prompts/commands yourself |
| **T2** | + `CLAUDE.md` / `AGENTS.md` guidance | the agent should know the review-gated propose→approve flow |
| **T3** | + slash commands | operators want one-keystroke propose / review / search |
| **T4** | + host hooks / settings | full adoption: session hooks, settings wired in |

Run `vouch install-mcp --list` to see the adapter catalogue. As of vouch 1.0.0
it covers claude-code, claude-desktop, cline, codex, continue, cursor, openclaw,
windsurf, and zed.

## What the script does

1. `install-mcp --list` — print the adapter catalogue. Read-only; touches no
   project tree.
2. `init` a throwaway project — seeds the `.vouch/` KB and a starter claim.
3. `install-mcp claude-code --tier T1 --path <tmp>` — writes **only** the MCP
   wire (`.mcp.json`), prints the `+` file list, and leaves `CLAUDE.md`,
   slash commands, and host settings untouched (those are T2..T4).
4. With the wire in place, send the two discovery calls a host makes first —
   `kb.capabilities` (the method surface) and `kb.status` (what's in the KB) —
   through `vouch serve --transport jsonl`, the same transport the wire uses.
5. Show the human mirror: `vouch capabilities` returns the same surface for an
   operator eyeballing the install.

## Why it's a playbook, not auto-applied broadly

T1 here is genuinely safe (one file, throwaway dir) and the script runs it. But
the point of `install-mcp` is to wire vouch into a tree you commit, and T2..T4
mutate that tree. Run those tiers deliberately, against a project you intend to
keep, and review the diff before committing.

## Example output

```text
-- install-mcp claude-code --tier T1 (MCP wire only) --
  + .mcp.json
Done — 1 written, 0 appended, 0 skipped under /tmp/tmp.YG6gPc5VYB

-- files the T1 wire dropped into the project --
<project>/.mcp.json

-- response envelopes (responses.jsonl) --
{"id": "caps", "ok": true, "result": {"name": "vouch", "version": "1.0.0", ... "methods": ["kb.capabilities", "kb.status", ...]}}
{"id": "status", "ok": true, "result": {"kb_dir": ".../.vouch", "claims": 1, "pages": 1, "sources": 1, "pending_proposals": 0, "index_present": true}}

-- vouch capabilities (human mirror, method count) --
spec akbp-0.1-compatible (level 3) — 54 kb.* methods

-- assertions --
mcp wire ok (.mcp.json present)
kb.capabilities ok (54 methods discoverable)
kb.status ok (1 claim(s), 0 pending)

== vouch install-mcp playbook passed ==
```

## Methods demonstrated:

- `kb.capabilities` — the method surface a freshly-wired host discovers first
- `kb.status` — the KB summary a host reads to know what's there

Plus the CLI surface an operator drives by hand: `vouch install-mcp --list`,
`vouch install-mcp <host> --tier T1`, and `vouch capabilities` (the human
mirror of `kb.capabilities`).
