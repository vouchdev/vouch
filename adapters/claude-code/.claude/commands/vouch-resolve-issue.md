---
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
5. After the fix is committed, register the evidence first — claims cite
   content-hashed source ids, never raw `file:line` strings:
   `kb_register_source_from_path` on the offending file (and on the patch
   file or commit message). Then propose **at most three** new claims via
   `kb_propose_claim`, each citing those source ids in `evidence`, that
   capture:
   * the root cause in one sentence (name the file:line in the claim
     *text*; cite the registered source id),
   * the chosen fix pattern (cite the patch source id), and
   * any policy/precedent established (only if novel).

Do not auto-approve. Leave the proposals in `.vouch/proposed/` for the
maintainer to review with `vouch approve` after the PR merges.
