#!/usr/bin/env bash
# propose-family — exercise every proposal entry point plus the path-based
# source registrar, then show the leftover artifacts sitting PENDING behind
# the review gate.
set -euo pipefail

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

export VOUCH_AGENT=example-agent

echo "== init a fresh KB =="
"$VOUCH" init --path "$KB" >/dev/null
cd "$KB"
echo "KB at $KB/.vouch"
echo

# A source must already live under the KB root before register_source_from_path
# can hash it — that's the whole point of the "safe sibling" of inline
# register_source: it never reads outside the reviewed workspace.
mkdir -p "$KB/notes"
cat > "$KB/notes/acme-brief.md" <<'NOTE'
# acme-example brief

acme-example is a fictional org. alice-example works there.
NOTE

echo "== kb.register_source_from_path (JSONL) — durable immediately, no gate =="
# The JSONL transport has no per-request path; it resolves the KB from cwd,
# which we cd'd into above. A source is content-addressed and durable on write
# (it is evidence, not an assertion), so there is no proposal to approve here.
SRC_JSON="$(printf '%s\n' \
  '{"id":"src","method":"kb.register_source_from_path","params":{"path":"notes/acme-brief.md","title":"acme-example brief"}}' \
  | "$VOUCH" serve --transport jsonl)"
echo "$SRC_JSON"
# Pull the source id (a content sha256) out of the response without needing jq.
SRC="$(printf '%s' "$SRC_JSON" | grep -o '"id": *"[a-f0-9]\{64\}"' | head -n1 | grep -o '[a-f0-9]\{64\}')"
echo "registered source: $SRC"
echo

echo "== kb.propose_entity x2 (CLI) =="
E_ORG="$("$VOUCH" propose-entity --name acme-example --type company)"
E_PERSON="$("$VOUCH" propose-entity --name alice-example --type person)"
echo "org proposal:    $E_ORG"
echo "person proposal: $E_PERSON"
echo

# Ordering: a relation and a page reference their endpoints by id, and the
# proposal layer checks those endpoints already EXIST (are approved) — a
# dangling reference is rejected at propose time, not silently at approve.
# So the two entities have to clear the gate before we can wire anything to
# them. Approve them, capturing the durable entity ids.
# The proposer cannot approve their own write (forbidden_self_approval) — the
# review gate needs a second actor. A human reviewer runs the approve under
# their own id, which we set via VOUCH_AGENT for just these two calls.
echo "== approve the two entities (as a separate reviewer) so they can be referenced =="
ENT_ORG="$(VOUCH_AGENT=reviewer-example "$VOUCH" approve "$E_ORG" 2>&1 | sed -n 's#.*entity/##p')"
ENT_PERSON="$(VOUCH_AGENT=reviewer-example "$VOUCH" approve "$E_PERSON" 2>&1 | sed -n 's#.*entity/##p')"
echo "durable org entity:    $ENT_ORG"
echo "durable person entity: $ENT_PERSON"
echo

echo "== kb.propose_relation (CLI) — endpoints now exist =="
REL="$("$VOUCH" propose-relation --from "$ENT_PERSON" --rel works_at --to "$ENT_ORG")"
echo "relation proposal: $REL"
echo

echo "== kb.propose_claim (CLI) — cites the durable source =="
CLAIM="$("$VOUCH" propose-claim \
  --text "alice-example works at acme-example." \
  --source "$SRC" \
  --type observation)"
echo "claim proposal: $CLAIM"
echo

echo "== kb.propose_page (CLI, body from stdin) — links the durable entities =="
PAGE="$("$VOUCH" propose-page --title "overview" --type concept --body - \
  --entity "$ENT_PERSON" --entity "$ENT_ORG" \
  <<<'acme-example overview. see the works_at relation for the alice-example link.')"
echo "page proposal: $PAGE"
echo

echo "== kb.list_pending (JSONL) — the gate is still closed on the three writes =="
printf '%s\n' \
  '{"id":"pend","method":"kb.list_pending","params":{}}' \
  | "$VOUCH" serve --transport jsonl
echo

echo "== vouch pending — human view of the same queue =="
"$VOUCH" pending
echo

echo "== status: 2 entities + 1 source durable; relation/claim/page still pending =="
"$VOUCH" status

echo
echo "propose-family example passed"
