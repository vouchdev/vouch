#!/usr/bin/env bash
set -euo pipefail

# audit-timeline — read the authoritative event stream back after a
# register / propose / reject / approve / supersede sequence.
#
# vouch overridable so CI can point at a specific build:
#   VOUCH=/path/to/vouch bash run.sh
VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

# two identities so the review gate is real: the agent proposes writes,
# a separate reviewer decides them. vouch refuses self-approval by default.
export VOUCH_AGENT=example-agent

# run a vouch command as the human reviewer instead of the agent.
as_reviewer() { VOUCH_AGENT=alice-example "$VOUCH" "$@"; }

section() { printf '\n=== %s ===\n' "$1"; }

section "init a fresh KB"
"$VOUCH" init --path "$KB" >/dev/null
cd "$KB"

# a source must point at a real file; write one to cite.
SRC_FILE="$KB/acme-example-rfc.md"
cat > "$SRC_FILE" <<'EOF'
# acme-example auth RFC

the acme-example service issues short-lived access tokens.
the original draft set the token ttl to 30 minutes.
a later revision shortened it to 15 minutes.
EOF

section "register a source (kb.register_source)"
SRC_ID="$("$VOUCH" source add "$SRC_FILE" --title "acme-example auth RFC" --type file)"
echo "source: $SRC_ID"

section "propose two claims (kb.propose_claim)"
# claim A: the stale 30-minute value (will be superseded later)
PROP_A="$("$VOUCH" propose-claim \
  --text "acme-example access tokens expire after 30 minutes." \
  --source "$SRC_ID" --type observation --confidence 0.7 \
  --rationale "from the original rfc draft")"
echo "proposal A (30min): $PROP_A"

# claim B: a sloppy claim we will reject
PROP_B="$("$VOUCH" propose-claim \
  --text "acme-example tokens never expire." \
  --source "$SRC_ID" --type observation --confidence 0.4 \
  --rationale "misread of the rfc")"
echo "proposal B (bogus): $PROP_B"

section "reject the bogus proposal (kb.reject)"
as_reviewer reject "$PROP_B" --reason "contradicts the rfc — tokens are short-lived"

section "approve the 30-minute claim (kb.approve)"
CLAIM_A="$(as_reviewer approve "$PROP_A" | sed -n 's/.*claim\///p')"
echo "approved claim: $CLAIM_A"

section "propose + approve the corrected 15-minute claim"
PROP_C="$("$VOUCH" propose-claim \
  --text "acme-example access tokens expire after 15 minutes." \
  --source "$SRC_ID" --type observation --confidence 0.9 \
  --rationale "from the revised rfc")"
CLAIM_C="$(as_reviewer approve "$PROP_C" | sed -n 's/.*claim\///p')"
echo "approved claim: $CLAIM_C"

section "supersede the stale claim with the corrected one"
as_reviewer supersede "$CLAIM_A" "$CLAIM_C"

# ---- read the authoritative history back -------------------------------
section "audit timeline (vouch audit --tail 20)"
# one line per mutation: kb.init, source.add, proposal.create,
# proposal.reject, proposal.approve, claim.supersede — each attributed to
# an actor with the object ids it touched. append-only; never hand-edited.
"$VOUCH" audit --tail 20

section "structured form (vouch audit --json --tail 50)"
# the same events as JSON: event / actor / object_ids / timestamp.
"$VOUCH" audit --json --tail 50

section "done"
echo "every gate decision above is one immutable line in audit.log.jsonl."
