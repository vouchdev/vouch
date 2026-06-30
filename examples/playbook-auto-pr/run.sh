#!/usr/bin/env bash
set -euo pipefail

# playbook-auto-pr: open N mergeable PRs against a repo — cross-verified.
#
# this is a PLAYBOOK, not a live demo. `vouch auto-pr` clones/forks a github
# repo, sources its open issues, spawns the claude and codex CLIs to fix and
# cross-verify each item, runs the repo's own test gate, and finally calls
# `gh pr create`. it spawns external coding agents and hits GitHub over the
# network — so this script does NOT run it. it prints the exact command and
# the external-tool requirements, then exits 0.
#
# auto-pr is a SIBLING tool to the KB: it never writes to the vouch KB. to
# prove the KB surface is untouched, this script makes one read-only
# `kb.capabilities` call against a throwaway KB and shows the method is there.
#
# override the binary by exporting VOUCH (defaults to whatever `vouch` is on
# PATH):
#   VOUCH=/path/to/vouch bash run.sh

VOUCH="${VOUCH:-vouch}"

section() { printf '\n=== %s ===\n' "$1"; }

REPO_URL="https://github.com/acme-example/widget"

section "what auto-pr does"
cat <<'TXT'
vouch auto-pr opens N mergeable pull requests against a github repo:

  1. source work    — open issues first (optionally filtered by --issue-label),
                       then agent-discovered improvements.
  2. bootstrap skill — if the repo ships no contribution guidance, auto-pr
                       learns one from the repo's own merged PRs.
  3. fix + verify    — claude fixes; codex reviews (or vice versa), revising up
                       to --max-revise rounds until the verifier signs off.
  4. test gate       — the repo's OWN test suite must go green.
  5. open PR         — only then does `gh pr create` run.

auto-pr is a sibling tool. it touches NO KB state — no proposals, no claims,
no audit entries. the review gate it relies on is the upstream repo's PR
review, not vouch's. nothing here lands in your knowledge base.
TXT

section "the command (NOT run here)"
printf '  %s auto-pr "%s" \\\n' "$VOUCH" "$REPO_URL"
printf '      --workspace ./work --count 1 --dry-run\n'
printf '\n(--dry-run runs every stage EXCEPT git push / gh pr create.)\n'

section "requirements"
cat <<'TXT'
requires gh + claude + codex CLIs and network; not auto-run in this example.
auto-pr spawns external coding agents and pushes branches / opens PRs against
a real GitHub repo, so it is unsafe to execute unattended in an examples
harness. run it yourself against a repo you own.
TXT

# prove the sibling-tool boundary: auto-pr writes nothing to the KB, and a
# read-only kb.capabilities call against a fresh KB still reports the same
# surface. build a throwaway KB and ask it what it can do.
section "kb.capabilities — the KB surface is unaffected by auto-pr"
KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT
"$VOUCH" init --path "$KB" >/dev/null
export VOUCH_KB_PATH="$KB/.vouch"
export VOUCH_AGENT=example-agent

# the CLI mirror of kb.capabilities. auto-pr never appears as a write path
# here — it is not a kb.* method at all.
"$VOUCH" capabilities \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); m=d.get("methods",[]); print("kb methods advertised:", len(m)); print("kb.capabilities present:", "kb.capabilities" in m); print("any auto-pr write method on the KB surface:", any("auto" in x for x in m))'

section "the command's --help (real flags)"
if command -v "$VOUCH" >/dev/null 2>&1; then
  "$VOUCH" auto-pr --help || true
else
  echo "(vouch not on PATH; set VOUCH=/path/to/vouch to print --help)"
fi

printf '\nplaybook-auto-pr: printed the command, spawned no engines, opened no PR,\n'
printf 'mutated no KB (kb.capabilities is read-only).\n'
exit 0
