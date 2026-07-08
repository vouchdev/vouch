---
name: vouch-propose-from-pr
description: Distill a merged PR into vouch claim proposals
---

# /vouch-propose-from-pr

A PR is a decision: someone proposed a change, the team accepted it. The
"why" gets compressed into the merge message and forgotten. This command
preserves the why as cited claims in the KB.

Steps:

1. Parse `$ARGUMENTS` as a PR URL or `<owner>/<repo>#<num>`; default to the
   most-recently-merged PR you authored.
2. Fetch the PR title, body, and the merged-commit SHA via `gh`.
3. Register the merge commit as a `kb_register_source` so subsequent claims
   can cite it.
4. Read the diff. For each *behavioural* change (not formatting / renaming),
   draft one `kb_propose_claim` whose text summarises the new invariant the
   code now upholds, citing the source from step 3.
5. Propose at most five claims per PR. If a PR is that big, suggest the
   contributor split it next time.

Do not auto-approve. The KB's review gate is intentional; this command
only fills the queue.
