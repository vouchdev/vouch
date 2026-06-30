#!/usr/bin/env bash
set -euo pipefail

# review-gate-flow: the load-bearing invariant, end to end.
#
# every durable write in vouch passes through a human review gate. this
# script registers a source, proposes two claims, shows the pending queue,
# rejects one with a reason, approves the other into a durable claim, and
# confirms it now turns up in search.
#
# override the binary by exporting VOUCH (defaults to whatever `vouch` is
# on PATH):
#   VOUCH=/path/to/vouch bash run.sh

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

export VOUCH_AGENT=example-agent

section() { printf '\n=== %s ===\n' "$1"; }

section "init a fresh KB"
"$VOUCH" init --path "$KB"
cd "$KB"

section "register a source"
# every claim must cite evidence. register a file as a Source first; the
# command prints the source's sha256 id, which we capture.
printf 'auth design notes for acme-example.\nauth uses jwt, signed rs256.\n' > note.txt
SRC="$("$VOUCH" source add note.txt --title 'acme-example auth notes')"
echo "source id: $SRC"

section "propose two claims (nothing durable yet)"
# proposals are NOT durable writes — they sit in the pending queue until a
# human decides. propose-claim prints the proposal id.
PID1="$("$VOUCH" propose-claim --text 'auth uses jwt' --source "$SRC")"
PID2="$("$VOUCH" propose-claim --text 'sessions live forever' --source "$SRC")"
echo "proposal 1: $PID1"
echo "proposal 2: $PID2"

section "the review queue (2 pending)"
"$VOUCH" pending

section "reject the unverified claim, with a reason"
# rejection requires a reason — it becomes future-agent context, recorded
# in the audit log.
"$VOUCH" reject "$PID2" --reason 'unverified, contradicts ttl policy'

section "approve the good claim into a durable artifact"
# the gate refuses self-approval: the agent that proposed a claim cannot
# approve it. a different actor (the human reviewer) approves. VOUCH_AGENT
# is the actor each surface attributes the action to.
VOUCH_AGENT=alice-example "$VOUCH" approve "$PID1"

section "status: 1 durable claim, 0 pending"
"$VOUCH" status

section "search finds the approved claim"
# only approved, durable claims are indexed for retrieval. the rejected one
# never made it past the gate.
"$VOUCH" search jwt

printf '\nreview-gate-flow example passed\n'
