---
description: Recall what the project's vouch KB knows about a topic
---

# /vouch-recall

Use vouch's `kb_context` MCP tool to assemble a working set of claims, sources,
and entities the KB already has on the current topic. Print them with their
ids and citations; do not write anything.

Steps:

1. Call `kb_context` with `query: "$ARGUMENTS"`.
2. List every returned claim by id + one-line text; for each, show the
   source ids it cites.
3. End with a one-sentence summary of what's *missing* from the KB on this
   topic — the gap the user can fill with `/vouch-propose-from-pr` or
   `kb_propose_claim`.

Be terse. The KB is meant to remove ambiguity, not pad it.
