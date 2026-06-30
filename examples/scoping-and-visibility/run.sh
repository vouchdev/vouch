#!/usr/bin/env bash
# Viewer scoping: project and agent filters.
#
# vouch's retrieval surfaces (search, context, audit) are thin viewports over
# the storage layer. Each accepts a *viewer* (--project / --agent) and filters
# what that caller can see based on each artifact's scope. The artifacts on
# disk never change — only what a given viewer is shown.
#
# This script seeds four claims at different scopes, then runs the same query
# as different viewers and shows the hit sets diverge. It also shows the audit
# log narrowing per viewer, and status for the baseline counts.
set -euo pipefail

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

echo "=== vouch scoping-and-visibility example ==="
echo "kb: $KB"
echo

# --- build a fresh KB ----------------------------------------------------
"$VOUCH" init --path "$KB" >/dev/null
cd "$KB"
# CLI honours VOUCH_AGENT as the proposing actor; a different actor reviews,
# since vouch refuses self-approval by default.
export VOUCH_AGENT=example-agent

# A single shared source so every claim is citable (claims require evidence).
printf 'acme-example onboarding runbook v3\n' > "$KB/runbook.md"
SID="$("$VOUCH" source add "$KB/runbook.md" --title 'acme-example runbook')"

# --- seed four claims, then approve as a human reviewer -------------------
echo "--- seeding + approving four claims ---"
PUB="$(  "$VOUCH" propose-claim --text 'deploy database backups nightly across all regions' --source "$SID")"
ACME="$( "$VOUCH" propose-claim --text 'acme-example prod database lives in us-east-1'        --source "$SID")"
OTHER="$("$VOUCH" propose-claim --text 'other-example prod database lives in eu-west-1'       --source "$SID")"
PRIV="$( "$VOUCH" propose-claim --text 'alice-example secret rotation token refreshes hourly' --source "$SID")"

# approve as 'reviewer' (VOUCH_AGENT is the proposer; self-approval is blocked)
env -u VOUCH_AGENT VOUCH_USER=reviewer "$VOUCH" approve "$PUB" "$ACME" "$OTHER" "$PRIV" >/dev/null
echo "approved 4 claims"
echo

# --- set each claim's scope ----------------------------------------------
# vouch artifacts are plaintext yaml by design. scope is a metadata field on
# the durable claim — visibility (public | team | project | private) plus an
# optional project / agent binding. we set three of the four to non-default
# scopes, then rebuild the derived index so retrieval reflects them.
echo "--- scoping the claims ---"
python3 - "$KB" <<'PY'
import sys, glob, yaml
kb = sys.argv[1]
def scope(slug, sc):
    path = glob.glob(f"{kb}/.vouch/claims/{slug}.yaml")[0]
    doc = yaml.safe_load(open(path))
    doc["scope"] = sc
    yaml.safe_dump(doc, open(path, "w"), sort_keys=False)
# public claim left at default (visibility: project, project: null) => visible to all.
scope("acme-example-prod-database-lives-in-us-east-1",
      {"visibility": "project", "project": "acme-example", "agent": None})
scope("other-example-prod-database-lives-in-eu-west-1",
      {"visibility": "project", "project": "other-example", "agent": None})
scope("alice-example-secret-rotation-token-refreshes-hourly",
      {"visibility": "private", "project": None, "agent": "alice-example"})
print("  deploy-database-backups...            -> default (visible to everyone)")
print("  acme-example-prod-database...         -> project: acme-example")
print("  other-example-prod-database...        -> project: other-example")
print("  alice-example-secret-rotation-token   -> private, agent: alice-example")
PY
"$VOUCH" index >/dev/null
echo

ids() { python3 -c 'import sys,json; print([h["id"] for h in json.load(sys.stdin)["hits"]])'; }

# --- the same search, three viewers --------------------------------------
echo "--- kb.search 'database', filtered by viewer ---"
echo "viewer --project acme-example  (public + acme):"
echo -n "  "; "$VOUCH" search database --project acme-example --json | ids
echo "viewer --project other-example (public + other):"
echo -n "  "; "$VOUCH" search database --project other-example --json | ids
echo "no viewer                      (public only; project/private hidden):"
echo -n "  "; "$VOUCH" search database --json | ids
echo

# --- a private, agent-scoped claim ---------------------------------------
echo "--- kb.search 'token', a private claim ---"
echo "viewer --agent alice-example   (SHOULD see the private claim):"
echo -n "  "; "$VOUCH" search token --agent alice-example --json | ids
echo "viewer --project acme-example  (must NOT see it):"
echo -n "  "; "$VOUCH" search token --project acme-example --json | ids
echo

# --- kb.context applies the same scope filter ----------------------------
echo "--- kb.context 'database', per viewer ---"
echo "viewer --agent alice-example, context item ids:"
echo -n "  "; "$VOUCH" context database --agent alice-example \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("viewer", d["viewer"], "items", [i.get("id") for i in d["items"]])'
echo

# --- kb.audit narrows per viewer too -------------------------------------
echo "--- kb.audit narrows per viewer (viewer line on stderr) ---"
# unset VOUCH_AGENT here so the viewer reflects only --project, not the
# proposing agent that env var would otherwise supply.
echo "audit --project acme-example  (other-example's approval is hidden):"
env -u VOUCH_AGENT "$VOUCH" audit --project acme-example --tail 20 2>&1 \
    | grep -E 'viewer:|approve' | sed 's/^/  /'
echo
echo "event counts per viewer (kb.audit --json):"
echo -n "  acme-example:  "; env -u VOUCH_AGENT "$VOUCH" audit --project acme-example  --json | python3 -c 'import sys,json; print(len(json.load(sys.stdin)),"events")'
echo -n "  other-example: "; env -u VOUCH_AGENT "$VOUCH" audit --project other-example --json | python3 -c 'import sys,json; print(len(json.load(sys.stdin)),"events")'
echo

# --- kb.status: the unfiltered baseline ----------------------------------
echo "--- kb.status (baseline counts; scope is a viewer concern, not storage) ---"
"$VOUCH" status | sed 's/^/  /'
echo

echo "=== scoping-and-visibility example passed ==="
