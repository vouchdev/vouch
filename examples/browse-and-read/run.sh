#!/usr/bin/env bash
set -euo pipefail

# Browse and read every artifact kind.
#
# The read-side mirror of the propose family. This script builds a small KB
# holding one of each artifact kind (source, claim, page, entity, relation),
# then walks the five list_* enumerators, the four read_* fetchers, and cite.
#
# Override the binary with VOUCH=/path/to/vouch (defaults to `vouch` on PATH).

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
export VOUCH_KB_PATH="$KB/.vouch"
export VOUCH_AGENT=example-agent
export PYTHONUTF8=1

# NB: no `trap ... EXIT` here — bash fires EXIT traps when the command-
# substitution subshells below exit, which would delete the KB mid-run.
# We clean up explicitly at the end instead.

# proposals are written as VOUCH_AGENT (example-agent); the review gate
# forbids self-approval, so approvals run as a separate reviewer identity.
jsonl() { "$VOUCH" serve --transport jsonl; }
review() { VOUCH_AGENT=reviewer-example "$VOUCH" serve --transport jsonl; }

# pull result.<field> out of a single JSONL response line by id
field() {
  python3 -c '
import json, sys
want_id, path = sys.argv[1], sys.argv[2].split(".")
out = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    if row.get("id") == want_id:
        cur = row["result"]
        for k in path:
            cur = cur[k]
        out = cur
print(out)
' "$1" "$2"
}

# pull a field from the first element of a list-typed result
first_field() {
  python3 -c '
import json, sys
want_id, key = sys.argv[1], sys.argv[2]
out = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    if row.get("id") == want_id:
        out = row["result"][0][key]
print(out)
' "$1" "$2"
}

echo "=== vouch browse-and-read example ==="
echo "KB: $KB/.vouch"
echo

"$VOUCH" init --path "$KB" >/dev/null

# --- build a KB with one of each artifact kind ---------------------------
echo "--- building KB (register source, propose + approve each kind) ---"

# 1. register a source, capture its id
SRC=$(printf '%s\n' \
  '{"id":"src","method":"kb.register_source","params":{"content":"Acme ships deploys behind a feature flag by default.","title":"acme-deploy-policy","url":"https://example.com/acme/deploy-policy","source_type":"file"}}' \
  | jsonl | field src id)
echo "source:   $SRC"

# 2. propose + approve a claim citing that source
CLAIM_PROP=$(printf '%s\n' \
  '{"id":"pc","method":"kb.propose_claim","params":{"text":"Acme ships deploys behind a feature flag by default.","evidence":["'"$SRC"'"],"claim_type":"observation","confidence":0.9}}' \
  | jsonl | field pc proposal_id)
CLAIM=$(printf '%s\n' \
  '{"id":"ap","method":"kb.approve","params":{"proposal_id":"'"$CLAIM_PROP"'"}}' \
  | review | field ap id)
echo "claim:    $CLAIM"

# 3. propose + approve two entities (so a relation has endpoints)
ALICE_PROP=$(printf '%s\n' \
  '{"id":"pe","method":"kb.propose_entity","params":{"name":"alice-example","entity_type":"person","description":"example release owner"}}' \
  | jsonl | field pe proposal_id)
ALICE=$(printf '%s\n' \
  '{"id":"ae","method":"kb.approve","params":{"proposal_id":"'"$ALICE_PROP"'"}}' \
  | review | field ae id)

ACME_PROP=$(printf '%s\n' \
  '{"id":"pe2","method":"kb.propose_entity","params":{"name":"acme-example","entity_type":"company","description":"example org"}}' \
  | jsonl | field pe2 proposal_id)
ACME=$(printf '%s\n' \
  '{"id":"ae2","method":"kb.approve","params":{"proposal_id":"'"$ACME_PROP"'"}}' \
  | review | field ae2 id)
echo "entities: $ALICE  $ACME"

# 4. propose + approve a relation between the two entities
REL_PROP=$(printf '%s\n' \
  '{"id":"pr","method":"kb.propose_relation","params":{"src":"'"$ALICE"'","relation":"relates_to","target":"'"$ACME"'","confidence":0.9}}' \
  | jsonl | field pr proposal_id)
REL=$(printf '%s\n' \
  '{"id":"ar","method":"kb.approve","params":{"proposal_id":"'"$REL_PROP"'"}}' \
  | review | field ar id)
echo "relation: $REL"

# 5. propose + approve a page that links the claim
PAGE_PROP=$(printf '%s\n' \
  '{"id":"pp","method":"kb.propose_page","params":{"title":"Acme deploy policy","body":"Deploys ship behind a feature flag by default.","page_type":"concept","claim_ids":["'"$CLAIM"'"]}}' \
  | jsonl | field pp proposal_id)
PAGE=$(printf '%s\n' \
  '{"id":"app","method":"kb.approve","params":{"proposal_id":"'"$PAGE_PROP"'"}}' \
  | review | field app id)
echo "page:     $PAGE"
echo

# --- the read side: list_*, capture an id, then read_* -------------------

echo "=== kb.list_claims -> kb.read_claim ==="
LIST_OUT=$(printf '%s\n' '{"id":"lc","method":"kb.list_claims","params":{}}' | jsonl)
echo "$LIST_OUT"
CID=$(printf '%s' "$LIST_OUT" | first_field lc id)
printf '%s\n' '{"id":"rc","method":"kb.read_claim","params":{"claim_id":"'"$CID"'"}}' | jsonl
echo

echo "=== kb.list_pages -> kb.read_page ==="
LIST_OUT=$(printf '%s\n' '{"id":"lp","method":"kb.list_pages","params":{}}' | jsonl)
echo "$LIST_OUT"
PID=$(printf '%s' "$LIST_OUT" | first_field lp id)
printf '%s\n' '{"id":"rp","method":"kb.read_page","params":{"page_id":"'"$PID"'"}}' | jsonl
echo

echo "=== kb.list_entities -> kb.read_entity ==="
LIST_OUT=$(printf '%s\n' '{"id":"le","method":"kb.list_entities","params":{}}' | jsonl)
echo "$LIST_OUT"
EID=$(printf '%s' "$LIST_OUT" | first_field le id)
printf '%s\n' '{"id":"re","method":"kb.read_entity","params":{"entity_id":"'"$EID"'"}}' | jsonl
echo

echo "=== kb.list_relations -> kb.read_relation ==="
LIST_OUT=$(printf '%s\n' '{"id":"lr","method":"kb.list_relations","params":{}}' | jsonl)
echo "$LIST_OUT"
RID=$(printf '%s' "$LIST_OUT" | first_field lr id)
printf '%s\n' '{"id":"rr","method":"kb.read_relation","params":{"relation_id":"'"$RID"'"}}' | jsonl
echo

echo "=== kb.list_sources ==="
printf '%s\n' '{"id":"ls","method":"kb.list_sources","params":{}}' | jsonl
echo

# cite our claim ($CLAIM) specifically — it carries the source evidence we
# registered above, so cite resolves to a real source record.
echo "=== kb.cite (evidence backing the claim) ==="
printf '%s\n' '{"id":"ct","method":"kb.cite","params":{"claim_id":"'"$CLAIM"'"}}' | jsonl
echo

rm -rf "$KB"
echo "=== done. temp KB cleaned up. ==="
