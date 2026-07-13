---
name: vouch-resolve-issue
description: Use vouch's KB to ground a fix for a GitHub issue
---

# /vouch-resolve-issue

Wire vouch's `kb_context` into an issue-resolution flow: the KB should
inform the fix, and the act of solving should propose new claims that
make the next contributor faster.

Steps:

1. Parse `$ARGUMENTS` as a GitHub issue URL or `<owner>/<repo>#<num>` shorthand.
   If neither, ask for clarification.
2. `kb_context` with the issue title + body — what does the KB already know
   about this area? Show the top 5 claims.
3. Read the relevant code paths.
4. Propose the smallest fix (run the project's tests first to confirm the
   bug reproduces).
5. After the fix is committed, propose **at most three** new claims via
   `kb_propose_claim` that capture:
   * the root cause in one sentence (cited by the offending file:line),
   * the chosen fix pattern (cited by the patch commit), and
   * any policy/precedent established (only if novel).

Do not auto-approve. Leave the proposals in `.vouch/proposed/` for the
maintainer to review with `vouch approve` after the PR merges.
