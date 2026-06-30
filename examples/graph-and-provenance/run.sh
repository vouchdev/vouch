#!/usr/bin/env bash
set -euo pipefail

# graph-and-provenance: walk the graph and provenance surface — neighbors,
# why, trace, impact.
#
# vouch isn't just a pile of claims; the durable files form a typed graph.
# entities link to each other through approved relations, claims cite the
# source they came from, pages embed claims, newer claims supersede older
# ones, and every approval is an event in the audit log. four read-only
# commands let an agent walk that graph:
#
#   neighbors  — list the graph neighbors of any node (entity/claim/page/source)
#   why        — explain why a claim exists: cites, session, supersede chain, approval
#   trace      — shortest typed-edge path between two artifacts (exit 1 = no path)
#   impact     — what depends on a claim, and what breaks under a hypothetical op
#
# this script first builds a small connected graph (two entities + a relation,
# two release claims where v2 supersedes v1, a page that embeds v1, plus one
# unrelated claim), then runs each of the four commands against it.
#
# override the binary by exporting VOUCH (defaults to whatever `vouch` is on
# PATH):
#   VOUCH=/path/to/vouch bash run.sh

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

export VOUCH_AGENT=example-agent

section() { printf '\n=== %s ===\n' "$1"; }

# the review gate refuses self-approval: the proposer can't approve their own
# write. example-agent proposes; example-reviewer approves.
approve() { VOUCH_AGENT=example-reviewer "$VOUCH" approve "$@"; }

section "init a fresh KB"
"$VOUCH" init --path "$KB"
cd "$KB"

section "approve two entities + a relation between them"
# entity ids are slugs derived from --name. the relation type must be one of
# vouch's typed edges (owned_by here); it becomes a graph edge once approved.
E1="$("$VOUCH" propose-entity --name alice-example --type person)"
E2="$("$VOUCH" propose-entity --name acme-example --type company)"
approve "$E1" "$E2"
REL="$("$VOUCH" propose-relation --from alice-example --rel owned_by --to acme-example)"
approve "$REL"

section "register two sources, one per release"
printf 'alice-example shipped acme-example v1 on 2026-06-01.\n' > rn1.txt
printf 'alice-example shipped acme-example v2 on 2026-06-20.\n' > rn2.txt
S1="$("$VOUCH" source add rn1.txt --title release-note-v1)"
S2="$("$VOUCH" source add rn2.txt --title release-note-v2)"
echo "source v1: $S1"
echo "source v2: $S2"

section "propose two claims inside one session (jsonl), then approve"
# propose over the jsonl transport so the claims carry a session_id — that's
# the proposedIn edge `why` will surface. each claim cites its own source.
SESS="$("$VOUCH" session start --agent example-agent \
          --task 'map acme-example release provenance' | head -1)"
echo "session: $SESS"
REQ="$(printf '%s\n' \
  '{"id":"p1","method":"kb.propose_claim","params":{"text":"alice-example shipped acme-example v1","evidence":["'"$S1"'"],"session_id":"'"$SESS"'"}}' \
  '{"id":"p2","method":"kb.propose_claim","params":{"text":"alice-example shipped acme-example v2","evidence":["'"$S2"'"],"session_id":"'"$SESS"'"}}')"
OUT="$(printf '%s\n' "$REQ" | "$VOUCH" serve --transport jsonl)"
P1="$(printf '%s\n' "$OUT" | sed -n '1p' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["proposal_id"])')"
P2="$(printf '%s\n' "$OUT" | sed -n '2p' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["proposal_id"])')"
approve "$P1" "$P2"

section "v2 supersedes v1, and a page embeds v1"
# supersede records a claim->claim edge; the page embeds v1, giving a
# page->claim edge. now the two claims sit in one connected subgraph.
"$VOUCH" supersede alice-example-shipped-acme-example-v1 alice-example-shipped-acme-example-v2
PG="$("$VOUCH" propose-page --title 'acme-example release log' \
        --body 'release history for acme-example.' \
        --claim alice-example-shipped-acme-example-v1 \
        --entity acme-example)"
approve "$PG"

section "add one UNRELATED claim (for the no-path demo)"
printf 'launch-day weather was sunny.\n' > rn3.txt
S3="$("$VOUCH" source add rn3.txt --title weather-note)"
WC="$("$VOUCH" propose-claim --text 'the launch-day weather was sunny.' --source "$S3")"
approve "$WC"

section "neighbors — graph neighbors of the alice-example entity (depth 2)"
# always emits json. follows approved relations out from the node.
"$VOUCH" neighbors alice-example --depth 2

section "why — why does the v2 claim exist? (cites, session, supersede chain, approval)"
# expands provenance: the source it cites, the session it was proposed in,
# the approval event, and recursively the older claim it superseded.
"$VOUCH" why alice-example-shipped-acme-example-v2 --depth 4

section "trace — shortest typed-edge path from the v2 claim to the release-log page"
# v2 -> (supersedes) -> v1 -> (embeds) <- page. two hops across typed edges.
"$VOUCH" trace alice-example-shipped-acme-example-v2 --to acme-example-release-log

section "trace — a disconnected pair returns 'no path' and exits 1"
# the weather claim shares nothing with the release subgraph. we catch the
# non-zero exit so the script keeps going.
if "$VOUCH" trace alice-example-shipped-acme-example-v2 \
     --to the-launch-day-weather-was-sunny; then
  echo "unexpected: a path was found" >&2
  exit 1
else
  echo "(trace exited non-zero, as expected for a disconnected pair)"
fi

section "impact — what depends on v1, and what breaks if we archive it"
# dependents: the page that embeds it, and the v2 claim that superseded it.
# breakage is the set of ACTIVE pages embedding the claim; pages created via
# the CLI start as drafts, so archiving here is non-blocking (exit 0). were
# the page active, --if archive would list it as breakage and exit 1.
"$VOUCH" impact alice-example-shipped-acme-example-v1 --if archive

printf '\ngraph-and-provenance example passed\n'
