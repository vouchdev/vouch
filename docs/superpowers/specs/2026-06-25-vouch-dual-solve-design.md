# Design: `vouch dual-solve <issue-url>`

Status: approved (brainstorm), pending implementation plan
Date: 2026-06-25

## Summary

A new `vouch` CLI subcommand that takes a GitHub issue, runs **Claude Code**
and **Codex** on it independently in isolated git worktrees, shows the operator
both resulting diffs, lets them pick the winner, keeps that branch, and
**proposes** the chosen solution's rationale into the KB through the normal
review-gated flow.

It is a *sibling tool* in the same vein as the existing `vouch auto-pr` — it
orchestrates external coding engines. It differs from `auto-pr` in two ways:
the two engines are **independent competitors** (not fixer↔verifier), and the
**operator is the judge** (not the other engine). Unlike `auto-pr`, it does
write to the KB — but only ever as *proposals*, so the review-gate invariant
("every durable write goes through `proposals.approve`") is preserved.

## Decisions locked in brainstorming

| Question | Decision |
|---|---|
| Placement | Real `vouch` CLI subcommand (`cli.py` + new module). |
| Execution / artifact | Each engine runs headless in its own git worktree on a fresh branch; operator chooses between the two **diffs**. |
| KB record after choosing | Register winning commit as a `Source`, then propose **≤3** claims (decision, root cause, fix pattern), all cited. Gated — never auto-approved. |
| Grounding | Yes — inject identical `vouch context` (cited claims for the issue) into **both** engines' prompts for a fair comparison. |
| Failure handling | On a single-engine failure (error / timeout / empty diff): **prompt** the operator to proceed with the survivor or abort. Both fail → abort, non-zero exit. |
| Competitor shape | Fully **independent** — no cross-critique before the operator chooses. |

## Architecture

New module `src/vouch/dual_solve.py` plus one command in `src/vouch/cli.py`.

The entire subprocess boundary (git / gh / claude / codex) is funnelled through
the **existing** abstractions in `src/vouch/auto_pr.py`, which are already
unit-tested with a fake runner:

- `Runner` / `SubprocessRunner` — injectable subprocess boundary.
- `Engine` (with `.fix()`) — wraps `claude -p --permission-mode … --output-format json`
  and `codex exec … --sandbox …`; `engine_text()` extracts the reply.
- `claude_flags()` / `codex_flags()` — per-effort flags.
- `_require_engines()` — preflight check that git, gh, claude, codex are on PATH.
- `slugify()`, `git_diff()`, `commit_all()` — git helpers.

Where a helper must be shared between the two sibling tools, lift it into a
small `src/vouch/_engines.py` rather than cross-importing `auto_pr` from
`dual_solve` (keeps module boundaries clean; respects the repo's "pure I/O vs
business logic" altitude rules). If lifting is disruptive, fall back to
importing the shared names from `auto_pr` — decide during planning.

`dual_solve.py` holds the orchestration (issue fetch, grounding, dual run,
choose, resolve, KB record) and stays free of click; `cli.py` is the thin
command shell, matching how `auto_pr_cmd` delegates to `auto_pr.run_auto_pr`.

## Flow

1. **Preflight.** `_require_engines()`; discover the `.vouch/` root from cwd
   (same discovery the rest of the CLI uses).
2. **Fetch issue.** Accept a GitHub URL *or* `owner/repo#num` shorthand;
   `gh issue view <ref> --json number,title,body`. If neither parses, error.
3. **Ground (identical for both).** `build_context_pack(title + body)` →
   a cited-claims block prepended to one shared prompt string. Both engines
   receive byte-identical input, so the comparison is fair.
4. **Run both, isolated.** For each engine: `git worktree add` a fresh branch
   `vouch-dual/<issue-num>-<slug>-<engine>`, run `engine.fix()` constrained
   (claude `acceptEdits` / codex `workspace-write` by default; `--autonomy full`
   escalates with the same semantics as `auto-pr`), then `commit_all`.
5. **Failure handling.** If an engine errors, times out, or leaves an empty
   diff: print what happened and prompt *proceed with survivor / abort*. If
   **both** fail: clean up worktrees, error, non-zero exit.
6. **Choose.** Print a `--stat` summary for each diff, then the full diffs;
   prompt `[c]laude / [x]codex / [n]either`. `--json` emits both diffs +
   metadata instead of prompting (non-interactive).
7. **Resolve.** Keep the chosen branch; `git worktree remove` both worktrees;
   delete the loser's branch. "neither" discards both branches.
8. **Record to KB (gated).** Register the winning commit SHA as a `Source`
   (kind `commit`), then `propose_claim` ×(≤3): the **decision**
   ("for issue #N chose <engine> — <reason>"), the **root cause**, and the
   **fix pattern** — each citing that source. All land in `proposed/`.
   Print the `vouch approve <id>` next-steps. Nothing is auto-approved.

## CLI surface

```
vouch dual-solve <issue-url-or-owner/repo#num>
  --claude-effort {low,medium,high,max}   (default high)
  --codex-effort  {low,medium,high,max}   (default high)
  --autonomy      {edit,full}             (default edit)
  --reason TEXT        skip the "why did you pick this" prompt
  --no-record          keep the chosen branch, propose nothing to the KB
  --dry-run            run both engines but make no commits / KB writes
  --json               non-interactive: emit both diffs + metadata, no prompt
```

Flag names and defaults mirror `auto-pr` (`--claude-effort`, `--codex-effort`,
`--autonomy`, `--dry-run`, `--json`) so the two sibling tools feel consistent.

## Invariants preserved

- **Review gate.** The KB is only ever written via `propose_claim` →
  `proposed/`. Approval still requires a human `vouch approve`. No parallel
  path to `proposals.approve`.
- **Storage purity.** No business logic in `storage.py`; orchestration lives in
  `dual_solve.py`, KB writes go through `proposals.py`.
- **Citations enforced.** The winning commit is registered as a `Source` before
  any claim is proposed, so every proposed claim cites real evidence and passes
  validation.

## Testing

`tests/test_dual_solve.py`, fake-`Runner` driven (mirrors `tests/test_auto_pr.py`):

- both succeed → choose claude; chosen branch kept, loser branch removed.
- both succeed → choose codex.
- one empty diff → prompt path → proceed with survivor.
- both fail → abort, non-zero, worktrees cleaned up.
- `--no-record` keeps the branch and proposes nothing.
- recorded claims actually cite the registered commit source (validation passes).
- `--dry-run` makes no commits and no proposals.

Plus the `make check` gate: `pytest`, `mypy src`, `ruff check src tests`.

## Open / deferred

- **Module-sharing strategy** (`_engines.py` extraction vs. import from
  `auto_pr`) — settle in the implementation plan after reading `auto_pr` in
  full.
- **Scope/merge note for the PR.** `auto-pr` is documented as "never writes to
  the KB"; this tool does (proposes only). Call this out explicitly in the PR
  body so the maintainer can weigh it — it is the one point likely to draw
  review discussion. It does not violate the north star, but it widens the
  sibling-tool category.

## Out of scope (this iteration)

- More than two engines / configurable engine list.
- Cross-critique between the engines before the operator chooses.
- Opening PRs (that is `auto-pr`'s job); `dual-solve` is local-first up to the
  point the operator pushes the kept branch themselves.
