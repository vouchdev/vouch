#!/usr/bin/env bash
set -euo pipefail

# playbook-dual-solve: two engines, one reviewed diff.
#
# this is a PLAYBOOK, not a live demo. `vouch dual-solve` spawns the claude
# and codex CLIs in separate git worktrees and hits the network to fetch the
# issue — so this script does NOT run it. it prints the exact command and the
# external-tool requirements, then exits 0 without invoking any engine and
# without touching a KB. nothing here is auto-approved; see README.md for the
# review-gate flow that records the winning rationale.
#
# override the binary by exporting VOUCH (defaults to whatever `vouch` is on
# PATH); it is only used here to show --help, never to mutate state:
#   VOUCH=/path/to/vouch bash run.sh

VOUCH="${VOUCH:-vouch}"

section() { printf '\n=== %s ===\n' "$1"; }

ISSUE_URL="https://github.com/acme-example/widget/issues/42"

section "what dual-solve does"
cat <<'TXT'
vouch dual-solve runs two coding engines — claude and codex — on the SAME
github issue, each in its own git worktree on a fresh branch. you compare the
two diffs side by side, keep one branch, and (unless --no-record) the rationale
for your choice is proposed into the kb for review. the review gate is
untouched: the rationale lands as a PENDING proposal, nothing is auto-approved.
TXT

section "the command (NOT run here)"
printf '  %s dual-solve "%s" \\\n' "$VOUCH" "$ISSUE_URL"
printf '      --claude-effort high --codex-effort high\n'

section "requirements"
cat <<'TXT'
requires gh + claude + codex CLIs and network; not auto-run in this example.
dual-solve spawns external coding agents and fetches the issue over the
network, so it is unsafe to execute unattended in an examples harness.
TXT

section "after you pick a winner — review the proposed rationale"
cat <<'TXT'
the chosen branch stays in your worktree. the rationale is a pending proposal:

  vouch pending                 # see the rationale awaiting review
  vouch approve <proposal-id>   # a DIFFERENT actor approves it into a claim

a human reviewer (not the proposing agent) approves. that is the same review
gate every durable vouch write passes through — dual-solve adds no bypass.
TXT

# show the real flags so the playbook stays honest against the installed binary.
section "vouch dual-solve --help (real flags)"
if command -v "$VOUCH" >/dev/null 2>&1; then
  "$VOUCH" dual-solve --help || true
else
  echo "(vouch not on PATH; set VOUCH=/path/to/vouch to print --help)"
fi

printf '\nplaybook-dual-solve: printed the command, ran no engines, mutated no KB.\n'
exit 0
