#!/usr/bin/env bash
set -euo pipefail

# four search backends and a score floor.
#
# seeds a handful of approved claims, then runs the same query through each
# backend so you can watch the `backend` field in the json output report what
# actually ran. fts5 and substring always work; embedding and hybrid lean on
# the optional vouch-kb[embeddings] extra and degrade to a skip notice when it
# is absent.

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

export VOUCH_AGENT=example-agent

echo "== init a fresh kb =="
"$VOUCH" init --path "$KB" >/dev/null
cd "$KB"

# the review gate forbids self-approval, so propose as one agent and approve as
# a separate reviewer. every claim cites a recorded source.
NOTE="$KB/auth-notes.md"
printf 'auth design notes for acme-example\n' > "$NOTE"

echo
echo "== record a source and propose four claims (as alice-example) =="
export VOUCH_AGENT=alice-example
SID="$("$VOUCH" source add "$NOTE" --title "acme auth notes")"
P1="$("$VOUCH" propose-claim --type fact --source "$SID" \
  --text "JWT access tokens expire after 15 minutes at acme-example.")"
P2="$("$VOUCH" propose-claim --type fact --source "$SID" \
  --text "Refresh tokens rotate on every use and are stored hashed.")"
P3="$("$VOUCH" propose-claim --type fact --source "$SID" \
  --text "The JWT signing key is rotated quarterly via the secrets manager.")"
P4="$("$VOUCH" propose-claim --type fact --source "$SID" \
  --text "Session cookies are SameSite=strict and HttpOnly.")"

echo
echo "== approve them through the gate (as reviewer-example) =="
export VOUCH_AGENT=reviewer-example
"$VOUCH" approve "$P1" "$P2" "$P3" "$P4"
"$VOUCH" index >/dev/null

# small helper: print the backend + a hit count from --json output.
summarize() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
hits = d["hits"]
print("  backend={}  hits={}".format(d["backend"], len(hits)))
for h in hits:
    print("    {}/{}  score={}".format(h["kind"], h["id"], h["score"]))
'
}

echo
echo "== query 'jwt' through --backend fts5 =="
"$VOUCH" search jwt --backend fts5 --json | summarize

echo
echo "== query 'jwt' through --backend substring =="
"$VOUCH" search jwt --backend substring --json | summarize

# embedding + hybrid need the [embeddings] extra. detect it; only run those legs
# when it is installed so the example stays green on a base install.
echo
if python3 -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("sentence_transformers") else 1)' 2>/dev/null; then
  echo "== query 'jwt' through --backend embedding =="
  "$VOUCH" search jwt --backend embedding --json | summarize
  echo
  echo "== query 'jwt' through --backend hybrid --min-score 0.2 =="
  "$VOUCH" search jwt --backend hybrid --min-score 0.2 --json | summarize
else
  echo "== embedding + hybrid backends skipped =="
  echo "  embedding backend skipped: install vouch-kb[embeddings] for semantic + hybrid search"
fi

echo
echo "== citation coverage from vouch stats =="
"$VOUCH" stats --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
c = d["citations"]
cov = c["coverage_rate"]
cov = "n/a" if cov is None else "{:.0f}%".format(cov * 100)
print("  claims with valid citations: {}/{} ({})".format(
    c["claims_with_valid_citation"], c["claims_total"], cov))
'

echo
echo "search-backends example passed"
