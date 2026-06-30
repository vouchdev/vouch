#!/usr/bin/env bash
set -euo pipefail

# preview a write before it touches the queue.
#
# mirrors akbp's dry_run:true / approval_required / approved:true triad,
# expressed in vouch's own vocabulary over the jsonl transport:
#   - propose_claim with dry_run:true PREVIEWS, files nothing
#   - propose_claim (no dry_run) files a PENDING proposal
#   - only kb.approve turns that proposal into a durable claim
#
# the review gate cannot be bypassed: a dry-run never reaches the queue,
# and a pending proposal is not readable as a claim until approved.

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)/kb"
export VOUCH_AGENT=example-agent

cleanup() { rm -rf "$(dirname "$KB")"; }
trap cleanup EXIT

echo "=== init a fresh kb ==="
"$VOUCH" init --path "$KB"
cd "$KB"
echo

# every request goes through one serve --transport jsonl process so the
# sequence reads like a real adapter session. responses come back one
# json envelope per line, in request order.
echo "=== feed a request sequence to: $VOUCH serve --transport jsonl ==="
REQS="$(cat <<'JSONL'
{"id":"src","method":"kb.register_source","params":{"title":"acme-example release notes","content":"acme-example ships rollback paths before every production change.","url":"https://example.com/acme/release-notes"}}
{"id":"preview","method":"kb.propose_claim","params":{"dry_run":true,"text":"acme-example requires a rollback path before any production change.","evidence":["SRC_ID"],"claim_type":"observation","confidence":0.8,"rationale":"previewing the write — should not file anything"}}
{"id":"pending-after-preview","method":"kb.list_pending","params":{}}
{"id":"file","method":"kb.propose_claim","params":{"text":"acme-example requires a rollback path before any production change.","evidence":["SRC_ID"],"claim_type":"observation","confidence":0.8,"rationale":"now actually file it for review"}}
{"id":"pending-after-file","method":"kb.list_pending","params":{}}
{"id":"approve","method":"kb.approve","params":{"proposal_id":"PROP_ID","reason":"reviewed by alice-example"}}
{"id":"read","method":"kb.read_claim","params":{"claim_id":"CLAIM_ID"}}
JSONL
)"

# the source id is only known after register_source returns, so the
# sequence is fed in two passes: first register, then substitute the real
# ids into the remaining requests. a real adapter does the same thing —
# it reads each response before composing the next request.

run_jsonl() { printf '%s\n' "$1" | "$VOUCH" serve --transport jsonl; }

# vouch forbids self-approval: the agent that proposed a write cannot be the
# one that approves it. so the reviewer runs under a different actor — this
# is the review gate in action, not a workaround.
run_jsonl_as() { printf '%s\n' "$2" | VOUCH_AGENT="$1" "$VOUCH" serve --transport jsonl; }

# pass 1: register the source.
SRC_RESP="$(run_jsonl "$(printf '%s\n' "$REQS" | sed -n '1p')")"
SRC_ID="$(printf '%s' "$SRC_RESP" | PYTHONUTF8=1 python3 -c 'import json,sys;print(json.loads(sys.stdin.read())["result"]["id"])')"
echo "registered source: $SRC_ID"
echo

# pass 2: dry-run preview + first list_pending (substitute the source id).
PREVIEW_REQS="$(printf '%s\n' "$REQS" | sed -n '2,3p' | sed "s/SRC_ID/$SRC_ID/g")"
PREVIEW_RESP="$(run_jsonl "$PREVIEW_REQS")"
echo "--- dry_run:true preview, then list_pending ---"
printf '%s\n' "$PREVIEW_RESP"
echo

# pass 3: real propose (no dry_run) + list_pending.
FILE_REQS="$(printf '%s\n' "$REQS" | sed -n '4,5p' | sed "s/SRC_ID/$SRC_ID/g")"
FILE_RESP="$(run_jsonl "$FILE_REQS")"
echo "--- propose_claim (filed), then list_pending ---"
printf '%s\n' "$FILE_RESP"
PROP_ID="$(printf '%s' "$FILE_RESP" | PYTHONUTF8=1 python3 -c 'import json,sys
for line in sys.stdin:
    r=json.loads(line)
    if r["id"]=="file": print(r["result"]["proposal_id"])')"
echo
echo "filed proposal: $PROP_ID"
echo

# pass 4: approve, then read the now-durable claim.
APPROVE_REQS="$(printf '%s\n' "$REQS" | sed -n '6p' | sed "s/PROP_ID/$PROP_ID/g")"
APPROVE_RESP="$(run_jsonl_as alice-example "$APPROVE_REQS")"
CLAIM_ID="$(printf '%s' "$APPROVE_RESP" | PYTHONUTF8=1 python3 -c 'import json,sys;print(json.loads(sys.stdin.read())["result"]["id"])')"
READ_REQS="$(printf '%s\n' "$REQS" | sed -n '7p' | sed "s/CLAIM_ID/$CLAIM_ID/g")"
READ_RESP="$(run_jsonl "$READ_REQS")"
echo "--- approve, then read_claim ---"
printf '%s\n' "$APPROVE_RESP"
printf '%s\n' "$READ_RESP"
echo

echo "=== assert the gate held at every step ==="
PYTHONUTF8=1 python3 - "$PREVIEW_RESP" "$FILE_RESP" "$APPROVE_RESP" "$READ_RESP" <<'PY'
import json, sys

def rows(blob):
    return {r["id"]: r for r in (json.loads(l) for l in blob.splitlines() if l.strip())}

preview = rows(sys.argv[1])
filed   = rows(sys.argv[2])
approve = rows(sys.argv[3])
read    = rows(sys.argv[4])

# 1. dry-run previews without filing.
p = preview["preview"]
assert p["ok"], p
assert p["result"]["dry_run"] is True, p
assert p["result"]["status"] == "pending", p
print("ok  dry_run:true returned a preview (status=pending, dry_run=true)")

# 2. the preview left the queue empty.
empty = preview["pending-after-preview"]
assert empty["ok"], empty
assert empty["result"] == [], empty
print("ok  list_pending empty after the dry-run preview")

# 3. a real propose files exactly one pending proposal.
f = filed["file"]
assert f["ok"] and f["result"]["dry_run"] is False, f
after = filed["pending-after-file"]
assert len(after["result"]) == 1, after
print(f"ok  propose_claim filed 1 pending proposal: {f['result']['proposal_id']}")

# 4. approve produces a durable claim.
a = approve["approve"]
assert a["ok"] and a["result"]["kind"] == "claim", a
print(f"ok  kb.approve minted durable claim: {a['result']['id']}")

# 5. the durable claim is now readable.
r = read["read"]
assert r["ok"] and r["result"]["id"] == a["result"]["id"], r
assert r["result"]["status"] in ("working", "active"), r
print(f"ok  read_claim returned the durable claim (status={r['result']['status']})")

print()
print("the gate held: nothing durable existed until kb.approve ran.")
PY
echo
echo "=== done ==="
