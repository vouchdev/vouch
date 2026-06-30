#!/usr/bin/env bash
set -euo pipefail

# Agent sessions: start, volunteer, end, crystallize.
#
# Drives one full agent run over the newline-delimited JSON transport:
# session_start opens the run, the agent files two proposals tagged to the
# session, volunteer_context drains the relevance-ranked context vouch pushes
# mid-run, session_end closes the run and reports its proposal ids, and
# crystallize approves every pending proposal in the session at once and writes
# a session-summary page. list_pending is checked before (N) and after (0).
# Mirrors AKBP's session-start-harness plus a batch-approve finish.

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

export VOUCH_AGENT=example-agent

echo "== vouch sessions + crystallize example =="

# 1. init the KB; the JSONL server discovers the root from cwd.
echo
echo "-- init --"
"$VOUCH" init --path "$KB"
cd "$KB"

# 2. Two config opt-outs that keep this a readable single-agent demo:
#    - volunteer.threshold lowered so the seeded claim qualifies under the
#      fts5 backend (no embeddings in this throwaway KB);
#    - review.approver_role: trusted-agent so example-agent may approve its own
#      session proposals at crystallize time (the default gate forbids
#      self-approval — see the lifecycle examples for the two-agent flow).
cat >> "$KB/.vouch/config.yaml" <<'YAML'
volunteer:
  enabled: true
  threshold: 0.5
  throttle_seconds: 0
review:
  approver_role: trusted-agent
YAML

# 3. Seed one approved claim relevant to the upcoming task so volunteer_context
#    has something to offer. Each id is generated server-side, so capture it
#    from one envelope before referencing it in the next.
echo
echo "-- seed an approved claim (so volunteer_context has something to push) --"
run() { printf '%s\n' "$@" | "$VOUCH" serve --transport jsonl; }

SRC=$(run '{"id":"src","method":"kb.register_source","params":{"content":"acme-example deploy runbook: blue-green with a ten minute soak before cutover.","title":"deploy runbook","source_type":"file"}}' \
  | python3 -c 'import sys,json; print(json.loads(sys.stdin.readline())["result"]["id"])')
PID=$(run '{"id":"p","method":"kb.propose_claim","params":{"text":"acme-example deploys via blue-green with a ten minute soak before cutover.","evidence":["'"$SRC"'"],"claim_type":"workflow","confidence":0.9,"tags":["deploy"]}}' \
  | python3 -c 'import sys,json; print(json.loads(sys.stdin.readline())["result"]["proposal_id"])')
run '{"id":"a","method":"kb.approve","params":{"proposal_id":"'"$PID"'"}}' >/dev/null
echo "approved seed claim from source ${SRC:0:12}..."

# 4. The session flow runs against ONE persistent serve process: the volunteer
#    queue lives in process memory, so session_start and volunteer_context must
#    share a connection. The driver below sends one request, reads one response
#    envelope, and prints the parts that matter.
echo
echo "-- session flow (one persistent jsonl connection) --"
python3 - "$VOUCH" "$SRC" <<'PY'
import json, subprocess, sys

VOUCH, SRC = sys.argv[1], sys.argv[2]
proc = subprocess.Popen(
    [VOUCH, "serve", "--transport", "jsonl"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
)

def call(rid, method, params):
    proc.stdin.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    assert resp["ok"], f"{method} failed: {resp}"
    return resp["result"]

# session_start opens the run and kicks off volunteer evaluation for the task.
sess = call("start", "kb.session_start",
            {"task": "plan an acme-example blue-green deploy with a soak window"})
sid = sess["id"]
print(f"session_start  -> {sid}")

# The agent files two proposals, both tagged to the session.
p1 = call("c1", "kb.propose_claim", {
    "text": "deploy soak window should be raised to fifteen minutes for acme-example.",
    "evidence": [SRC], "claim_type": "workflow", "confidence": 0.8,
    "tags": ["deploy"], "session_id": sid,
})
p2 = call("c2", "kb.propose_claim", {
    "text": "acme-example rollbacks must drain the blue pool before promoting green.",
    "evidence": [SRC], "claim_type": "workflow", "confidence": 0.85,
    "tags": ["deploy"], "session_id": sid,
})
print(f"propose_claim  -> {p1['proposal_id']}")
print(f"propose_claim  -> {p2['proposal_id']}")

# volunteer_context drains the relevance-ranked offers vouch queued for the task.
vol = call("vol", "kb.volunteer_context", {"session_id": sid})
offers = vol["volunteers"]
print(f"volunteer_context -> {len(offers)} offer(s)")
for o in offers:
    print(f"    claim={o['claim_id']} relevance={o['relevance']:.2f}")

# Pending count before the batch approve.
before = call("lp", "kb.list_pending", {})
print(f"list_pending (before crystallize) -> {len(before)} pending")

# session_end closes the run and backfills its proposal ids.
ended = call("end", "kb.session_end", {"session_id": sid})
print(f"session_end    -> proposal_ids={ended['proposal_ids']}")

# crystallize approves every still-pending proposal in the session at once and
# writes a session-summary page linking the approved claims.
cr = call("cr", "kb.crystallize", {"session_id": sid, "write_summary_page": True})
print(f"crystallize    -> approved={cr['approved']}")
print(f"               -> summary_page_id={cr['summary_page_id']}")

# Pending count after: the session's proposals are gone.
after = call("lp2", "kb.list_pending", {})
print(f"list_pending (after crystallize)  -> {len(after)} pending")

proc.stdin.close()
proc.wait()

# Assertions — the contract a host mirrors.
assert offers and offers[0]["relevance"] >= 0.5, offers
assert len(before) == 2, before
assert sorted(ended["proposal_ids"]) == sorted([p1["proposal_id"], p2["proposal_id"]]), ended
assert len(cr["approved"]) == 2 and not cr.get("failures"), cr
assert cr["summary_page_id"], cr
assert len(after) == 0, after
print("assertions ok")
PY

echo
echo "== vouch sessions + crystallize example passed =="
