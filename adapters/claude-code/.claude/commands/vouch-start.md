---
description: Seed the current session from an approved vouch claim, page, or proposal
---

# /vouch-start

Seed the current session from a previous session's approved summary — or
from any other claim, page, or proposal the user points at. `$ARGUMENTS`
is the ref: a proposal id, an approved claim id, or an approved page id
(the vouch-ui Claims page hands users exactly this command). Read-only:
never approve, reject, or propose anything here.

Steps:

1. Run `vouch session start-from "$ARGUMENTS" --json` via Bash. It
   returns `{ref, title, status, seed}` where `seed` is a paste-ready
   context block. If the CLI is unavailable, fall back to MCP:
   `kb_read_claim` with the ref, then `kb_read_page` if that misses.
2. Adopt the seed as reviewed context and continue where that session
   left off. Treat the record as the authoritative baseline, but verify
   anything that may have gone stale — file paths, branch names, open
   PRs, pending decisions — against the current repo state before
   acting on it.
3. When the ref is a claim id, call `kb_why` on it. If the provenance
   edges carry an originating agent `session_id`, tell the user they
   can also resume the raw session with `claude --resume <session_id>`.
4. Confirm in one line what you seeded from (title, status, ref) and
   what you are doing next. If the ref resolves to nothing, say so and
   suggest `vouch session list` to find the right id.

The seed is a summary, not a transcript. Where it is thin, recall more
with `kb_context` or `/vouch-recall` before re-deriving.
