#!/usr/bin/env bash
# Claim lifecycle: supersede, contradict, archive, confirm.
#
# Builds a throwaway KB, pushes four claims through the review gate, then
# walks each claim through one lifecycle transition and reads the result
# back over the JSONL transport so the status flip is visible.
set -euo pipefail

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT
export VOUCH_AGENT=example-agent

section() { printf '\n=== %s ===\n' "$1"; }

# read_claim over the JSONL transport: emit one request, keep only the status.
# discover_root walks up from cwd, so this works because we cd into $KB below.
claim_status() {
  printf '{"id":"r","method":"kb.read_claim","params":{"claim_id":"%s"}}\n' "$1" \
    | "$VOUCH" serve --transport jsonl \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["result"]["status"], r["result"].get("last_confirmed_at"))'
}

section "init"
"$VOUCH" init --path "$KB"
cd "$KB"

section "register sources (every claim must cite evidence)"
# Each claim cites a real registered source; that is the review gate's
# minimum bar. Generic placeholder content only.
mk_source() {  # mk_source <filename> <body> -> prints source id
  local f="$KB/$1"
  printf '%s\n' "$2" > "$f"
  "$VOUCH" source add "$f" --title "$1"
}
S1=$(mk_source onboarding-note.md "acme-example onboarding: deploys run on fridays.")
S2=$(mk_source release-policy.md "acme-example release policy: deploys run on tuesdays.")
S3=$(mk_source acme-config.md "acme-example config snapshot: cache ttl 60 seconds.")
S4=$(mk_source acme-runtime.md "acme-example runtime probe: cache ttl 300 seconds.")

section "propose + approve four claims through the review gate"
# Two halves of a supersede pair (v1 -> v2) and a contradicting pair.
P1=$("$VOUCH" propose-claim --text "deploys run on fridays" \
      --source "$S1" --confidence 0.6)
P2=$("$VOUCH" propose-claim --text "deploys run on tuesdays" \
      --source "$S2" --confidence 0.9)
P3=$("$VOUCH" propose-claim --text "the cache ttl is 60 seconds" \
      --source "$S3")
P4=$("$VOUCH" propose-claim --text "the cache ttl is 300 seconds" \
      --source "$S4")

# The gate forbids self-approval: the agent proposes, a human reviewer
# approves. approve() prints "Approved -> claim/<id>"; pull the id off the arrow.
approve_one() {
  VOUCH_AGENT=human-reviewer "$VOUCH" approve "$1" | sed -E 's/.*claim\///'
}
C1=$(approve_one "$P1")
C2=$(approve_one "$P2")
C3=$(approve_one "$P3")
C4=$(approve_one "$P4")
printf 'c1=%s\nc2=%s\nc3=%s\nc4=%s\n' "$C1" "$C2" "$C3" "$C4"

section "supersede: c1 (fridays, v1) -> c2 (tuesdays, v2)"
echo "c1 before: $(claim_status "$C1")"
"$VOUCH" supersede "$C1" "$C2"
echo "c1 after:  $(claim_status "$C1")   # status flips to superseded"
echo "c2 stays:  $(claim_status "$C2")   # the live version is untouched"

section "contradict: c3 (ttl 60s) <-> c4 (ttl 300s)"
"$VOUCH" contradict "$C3" "$C4"
echo "contradiction relation recorded; both claims remain readable"

section "archive: c3 (the stale ttl) — kept for history, dropped from default retrieval"
echo "c3 before: $(claim_status "$C3")"
"$VOUCH" archive "$C3"
echo "c3 after:  $(claim_status "$C3")   # status flips to archived"

section "confirm: c4 (still-true ttl) — bumps last_confirmed_at"
echo "c4 before: $(claim_status "$C4")"
"$VOUCH" confirm "$C4"
echo "c4 after:  $(claim_status "$C4")   # last_confirmed_at moves forward"

section "audit chain is intact"
"$VOUCH" why "$C1" | sed -n '1,12p'

section "done"
echo "lifecycle example passed"
