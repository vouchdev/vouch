# Example session — automatic session capture

![vouch auto-capture demo](demo.gif)

A full **capture → review → commit → recall** loop, driven by Claude Code
hooks. A session works; vouch quietly harvests what it did into a gitignored
scratch buffer; at session end that buffer rolls up into a single **pending
summary proposal**; you approve it like any other write; the next session
starts with it injected. Nothing is auto-approved — the review gate stays
intact. Re-render the GIF with `vhs docs/demo.tape` (see [demo.tape](demo.tape)).

The capture path is fully mechanical — no LLM, no network, no agent
discipline required. It is wired by the Claude Code adapter's hooks
(`adapters/claude-code/.claude/settings.json`): `PostToolUse → vouch capture
observe`, `SessionEnd → vouch capture finalize`, and a `SessionStart` banner.

## Setup

```bash
$ mkdir acme-api && cd acme-api && git init -q
$ printf 'def verify(token):\n    return decode(token)\n' > src/auth.py
$ git add -A && git commit -q -m "chore: seed"
$ vouch init
Initialised KB at /tmp/acme-api/.vouch
Seeded starter claim: vouch-starter-reviewed-knowledge
Next steps:
  vouch status
  vouch search agent
  vouch serve
```

## 1. A session works — the PostToolUse hook harvests it

After every tool call, Claude Code's `PostToolUse` hook pipes the tool
payload to `vouch capture observe`. You never type these — the hook does.
Each call appends one compact observation (`{ts, tool, summary, files?,
cmd?}`) to a **gitignored scratch buffer**, deduped within a short window:

```bash
# what the hook runs after a Read, an Edit, a test run, and a grep:
$ echo '{"session_id":"sess-8f2e4c7a","tool_name":"Read","tool_input":{"file_path":"src/auth.py"}}' | vouch capture observe
$ echo '{"session_id":"sess-8f2e4c7a","tool_name":"Edit","tool_input":{"file_path":"src/auth.py"}}' | vouch capture observe
$ echo '{"session_id":"sess-8f2e4c7a","tool_name":"Bash","tool_input":{"command":"pytest -q"}}' | vouch capture observe
$ echo '{"session_id":"sess-8f2e4c7a","tool_name":"Grep","tool_input":{"pattern":"verify_signature"}}' | vouch capture observe
```

The buffer lives at `.vouch/captures/<session-id>.jsonl` — scratch, held
**outside** the KB, gitignored so it never pollutes history:

```bash
$ cat .vouch/captures/sess-8f2e4c7a.jsonl
{"files": ["src/auth.py"], "summary": "Read auth.py", "tool": "Read", "ts": 1782928132.1}
{"files": ["src/auth.py"], "summary": "Edited auth.py", "tool": "Edit", "ts": 1782928126.8}
{"cmd": "pytest -q", "summary": "Ran: pytest -q", "tool": "Bash", "ts": 1782928136.5}
{"summary": "Grep verify_signature", "tool": "Grep", "ts": 1782928141.1}
```

Observations are one-line summaries and file names — **not** full tool
output — so credentials and large blobs never get buffered.

## 2. Session ends — finalize rolls it into ONE pending proposal

At session end, the `SessionEnd` hook runs `vouch capture finalize`. It reads
the buffer, adds a `git diff` backstop (to catch edits `PostToolUse` missed),
and — if there are at least `capture.min_observations` (default 3) — files a
single **pending page proposal** through the normal review gate. It never
calls `approve()`.

```bash
$ echo '{"session_id":"sess-8f2e4c7a"}' | vouch capture finalize
{
  "_meta": {
    "vouch_trust": { "auth_subject": null, "caller_kind": "cli", "remote": false }
  },
  "captured": 5,
  "summary_proposal_id": "20260701-174914-a97e6a4d"
}
```

`captured: 5` = four harvested observations + one file from the git-diff
backstop. The buffer file is deleted; its contents now live only inside the
pending proposal, awaiting your review.

## 3. Next session start — the nudge

The `SessionStart` hook runs `vouch capture banner`, so the next time you open
a session in this workspace you see how many captured summaries are queued:

```bash
$ vouch capture banner
🔔 1 auto-captured session summary(ies) awaiting review — run `vouch review`.
```

## 4. Review the queue

Captured summaries are ordinary pending proposals — `vouch pending`, `vouch
review`, and the review-ui all show them, attributed to the `vouch-capture`
actor:

```bash
$ vouch pending
• 20260701-174914-a97e6a4d  [page]  by vouch-capture
    session summary: acme-api (sess-8f2e4c7a)
```

## 5. Approve → durable page

```bash
$ vouch approve 20260701-174914-a97e6a4d --reason "accurate summary"
Approved → page/session-summary-acme-api-sess-8f2e4c7a
```

The summary vouch kept is now a plain-markdown page on disk, committed
alongside your code (shown here inside a fence so its own headings render
literally):

````text
# session summary: acme-api (sess-8f2e4c7a)

- generated: 2026-07-01T17:49:14.372133+00:00
- session: `sess-8f2e4c7a`
- observations: 4

## files modified this session

- src/auth.py

## git changes

```
src/auth.py | 3 ++-
 1 file changed, 2 insertions(+), 1 deletion(-)
```

## activity

- Bash: 1
- Edit: 1
- Grep: 1
- Read: 1

## notable commands

- `pytest -q`

## observations

- Read auth.py
- Edited auth.py
- Ran: pytest -q
- Grep verify_signature
````

## 6. The next session starts with it — no amnesia

The `SessionStart` hook also runs `vouch recall`, which emits a digest of all
approved knowledge for injection into the new session's context. The summary
you just approved is now in it — the next session opens already knowing what
the last one did:

```text
$ vouch recall
<vouch-approved-knowledge>
# approved KB knowledge for this repo — 1 claim(s), 2 page(s). reviewed, cited, durable. use kb_read_page / kb_search for detail; kb_propose_* (human-approved) to add more.

## claims
- [vouch-starter-reviewed-knowledge] Vouch stores reviewed, cited knowledge in the repository so future agent sessions can retrieve agreed project context.

## pages
- [edit-in-obsidian] Edit in Obsidian
- [session-summary-acme-api-sess-8f2e4c7a] session summary: acme-api (sess-8f2e4c7a)
</vouch-approved-knowledge>
```

## 7. Commit

```bash
$ git add .vouch/ && git commit -m "kb: approve session summary"
```

What lands in git: the durable page, its decision record, the audit line.
What doesn't: the `.vouch/captures/` scratch buffer (gitignored) and
`state.db` (a derivable cache — `vouch index` rebuilds it).

## Notes

- **The review gate holds.** Capture harvests and rolls up automatically, but
  the only durable artifact is a `PENDING` proposal a human approves. There is
  no auto-approve and no trusted-agent shortcut on this path.
- **No LLM anywhere.** The rollup is pure heuristics — type counts, file
  names, a git-diff stat — so the hook stays fast and offline.
- **One summary per session**, not one per turn, so the queue doesn't flood.
  Individual claims an agent files via MCP during a session still coexist as
  their own pending items.
- **Config** lives under `capture.*` in `.vouch/config.yaml`:
  `capture.enabled` (default `true`), `capture.min_observations` (default `3`).
  Set `capture.enabled: false` to turn the whole path off.

## Next steps

- Wire vouch into Claude Code via `.mcp.json` and the adapter hooks (see
  [README](../README.md#wiring-into-claude-code)).
- Prefer filing knowledge as you go? Agents can still call
  `kb.propose_claim` / `kb.propose_page` directly over MCP — the manual write
  path and the automatic capture path share the same review gate.
- Export the KB as a portable bundle: `vouch export --out kb.tar.gz`.
