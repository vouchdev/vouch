# playbook-dual-solve/

Dual-solve: two engines, one reviewed diff.

`vouch dual-solve` runs the claude and codex coding agents on the *same*
github issue, each in its own git worktree on a fresh branch. the operator
compares the two diffs, keeps the winning branch, and the rationale for the
choice is proposed into the KB for review. the review gate is untouched —
the rationale lands as a **pending** proposal; nothing is auto-approved.

This is a **playbook**, not a live demo. dual-solve spawns external coding
agents (claude + codex) and fetches the issue over the network, so it is
unsafe to run unattended in an examples harness. `run.sh` prints the exact
command and the external-tool requirements, then exits 0 without invoking
any engine and without touching a KB.

## Run it

```bash
VOUCH=/path/to/vouch bash run.sh
```

`run.sh` prints the command, the `requires gh + claude + codex` notice, and
the real `vouch dual-solve --help` flags. it runs no engines and mutates no
KB. `VOUCH` defaults to whatever `vouch` is on your `PATH`.

To actually run dual-solve (outside this example), you need the `gh`, `claude`,
and `codex` CLIs installed and network access, then from inside the target git
repo:

```bash
vouch dual-solve "https://github.com/acme-example/widget/issues/42" \
    --claude-effort high --codex-effort high
```

## The flow

1. **two engines, two worktrees.** dual-solve fetches the issue, then spawns
   claude and codex on it, each in its own git worktree on a fresh branch.
   `--claude-effort` / `--codex-effort` (`low|medium|high|max`) tune how hard
   each engine works; `--autonomy edit` (the safe default) auto-accepts file
   edits only.
2. **compare the diffs.** dual-solve prints both candidate diffs side by side.
   the operator reads them and picks the winner (or `--json` emits both diffs
   + metadata for a non-interactive caller).
3. **keep one branch.** the chosen branch stays in the worktree; the losing
   branch is discarded. `--no-record` keeps the branch but proposes nothing.
4. **record the rationale — through the review gate.** unless `--no-record`,
   the rationale for the choice is filed as a **pending** proposal via the
   normal `kb.propose_claim` path. it is not a durable write yet.
5. **review and approve.** a human reviewer lists the queue and approves:

   ```bash
   vouch pending                 # see the rationale awaiting review
   vouch approve <proposal-id>   # a DIFFERENT actor approves it into a claim
   ```

   the gate refuses self-approval, so the agent that proposed the rationale
   cannot approve it — a human (or a different actor) does. dual-solve adds no
   bypass; it is a sibling to `auto-pr` and both honor the same gate.

## What you'll see

```
=== the command (NOT run here) ===
  vouch dual-solve "https://github.com/acme-example/widget/issues/42" \
      --claude-effort high --codex-effort high

=== requirements ===
requires gh + claude + codex CLIs and network; not auto-run in this example.

=== after you pick a winner — review the proposed rationale ===
  vouch pending                 # see the rationale awaiting review
  vouch approve <proposal-id>   # a DIFFERENT actor approves it into a claim

playbook-dual-solve: printed the command, ran no engines, mutated no KB.
```

## Methods demonstrated

- `kb.propose_claim` — the winning rationale is filed as a pending proposal
  (not a durable write).
- `kb.list_pending` — the operator lists the review queue (`vouch pending`).
- `kb.approve` — a different actor approves the rationale into a durable
  claim (`vouch approve <proposal-id>`), through the standard review gate.
