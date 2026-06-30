#!/usr/bin/env bash
set -euo pipefail

# provenance-graph-export: export the provenance DAG, then rebuild its cache.
#
# vouch tracks where every durable claim came from — which source it cites,
# which session proposed it, which decision approved it, which earlier claim
# it derives from. that's the provenance DAG. this script approves a few
# connected claims and a derived_from relation, renders the DAG as a mermaid
# flowchart and as graphviz dot, exports one session's subgraph over the
# jsonl transport, and finally rebuilds the derived prov_edges cache.
#
# the prov_edges table is *derived state*: it's a pure acceleration of the
# graph that lives in the durable yaml/md files. rebuilding it is always
# safe — it serialises exactly what the files already say.
#
# override the binary by exporting VOUCH (defaults to whatever `vouch` is on
# PATH):
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
# every claim cites evidence. register a file as a Source first; the command
# prints the source's sha256 id.
printf 'acme-example auth design notes.\nauth uses jwt, signed rs256.\n' > note.txt
SRC="$("$VOUCH" source add note.txt --title 'acme-example auth notes')"
echo "source id: $SRC"

section "start an agent session"
# the session id stamps the proposals raised inside it, so the DAG can later
# be sliced to "everything this one agent run produced".
SESS="$(VOUCH_AGENT=bob-example "$VOUCH" session start --agent bob-example \
          --task 'map acme-example auth provenance' | head -1)"
echo "session id: $SESS"

section "propose two connected claims inside the session (jsonl)"
# propose over the jsonl transport so we can pass session_id. bob-example is
# the proposer; the claims are pending until a different actor approves.
PROPOSE_REQ="$(printf '%s\n' \
  '{"id":"p1","method":"kb.propose_claim","params":{"text":"acme-example auth uses jwt","evidence":["'"$SRC"'"],"session_id":"'"$SESS"'"}}' \
  '{"id":"p2","method":"kb.propose_claim","params":{"text":"jwt is signed rs256","evidence":["'"$SRC"'"],"session_id":"'"$SESS"'"}}')"
PROPOSE_OUT="$(printf '%s\n' "$PROPOSE_REQ" | VOUCH_AGENT=bob-example "$VOUCH" serve --transport jsonl)"
PID1="$(printf '%s\n' "$PROPOSE_OUT" | sed -n '1p' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["proposal_id"])')"
PID2="$(printf '%s\n' "$PROPOSE_OUT" | sed -n '2p' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["proposal_id"])')"
echo "proposal 1: $PID1"
echo "proposal 2: $PID2"

section "approve both claims (different actor than the proposer)"
# the gate refuses self-approval: bob-example proposed, alice-example reviews.
# both claims now cite the same source and share the same session, so they
# sit in one connected provenance subgraph.
VOUCH_AGENT=alice-example "$VOUCH" approve "$PID1" "$PID2"

section "render the whole DAG as a mermaid flowchart"
# cites / approvedBy / proposedIn / derivedFrom edges, one node per artifact.
"$VOUCH" graph --format mermaid

section "render the whole DAG as graphviz dot"
"$VOUCH" graph --format dot

section "export just this session's subgraph (jsonl, scoped)"
# graph_export takes an optional session filter — the slice of the DAG that
# bob-example's run touched.
SCOPED_REQ='{"id":"g1","method":"kb.graph_export","params":{"format":"dot","session":"'"$SESS"'"}}'
printf '%s\n' "$SCOPED_REQ" | "$VOUCH" serve --transport jsonl \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"]["graph"])'

section "rebuild the derived prov_edges cache"
# prov_edges is derived state — a pure acceleration of the graph that lives
# in the durable files. rebuilding regenerates it in one pass and reports the
# edge count. always safe; never edits a source of truth.
"$VOUCH" provenance rebuild --json

printf '\nprovenance-graph-export example passed\n'
