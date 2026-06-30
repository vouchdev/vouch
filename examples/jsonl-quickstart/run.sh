#!/usr/bin/env bash
set -euo pipefail

# JSONL transport quickstart — the shortest adapter contract for a new host.
#
# Drives one newline-delimited JSON request sequence through
# `vouch serve --transport jsonl`: discovery -> status -> search -> context.
# Mirrors AKBP's jsonl-quickstart in shape.

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
REQUESTS="$(mktemp)"
RESPONSES="$(mktemp)"
trap 'rm -rf "$KB" "$REQUESTS" "$RESPONSES"' EXIT

export VOUCH_AGENT=example-agent

echo "== vouch JSONL quickstart example =="

# 1. init seeds a starter claim; the JSONL server discovers the KB from cwd.
echo
echo "-- init (seeds a starter claim) --"
"$VOUCH" init --path "$KB"
cd "$KB"

# 2. Build the request sequence: discovery -> status -> search -> context.
cat > "$REQUESTS" <<'JSONL'
{"id":"caps","method":"kb.capabilities","params":{}}
{"id":"status","method":"kb.status","params":{}}
{"id":"search","method":"kb.search","params":{"query":"agent","limit":5}}
{"id":"context","method":"kb.context","params":{"task":"what is this kb about","limit":5}}
JSONL

echo
echo "-- request sequence (requests.jsonl) --"
cat "$REQUESTS"

# 3. Pipe the sequence through the JSONL transport, capture the responses.
echo
echo "-- response envelopes (responses.jsonl) --"
"$VOUCH" serve --transport jsonl < "$REQUESTS" | tee "$RESPONSES"

# 4. Assert each envelope is ok:true and capabilities lists the method surface.
echo
echo "-- assertions --"
python3 - "$RESPONSES" <<'PY'
import json, sys

rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
by_id = {r["id"]: r for r in rows}

for rid in ("caps", "status", "search", "context"):
    r = by_id[rid]
    assert r["ok"], f"{rid} failed: {r}"

caps = by_id["caps"]["result"]
methods = caps["methods"]
assert len(methods) == 54, f"expected 54 methods, got {len(methods)}"
for m in ("kb.capabilities", "kb.status", "kb.search", "kb.context"):
    assert m in methods, f"{m} missing from capabilities.methods"
print(f"capability discovery ok ({len(methods)} methods)")

status = by_id["status"]["result"]
assert status["claims"] >= 1, status
print(f"status ok ({status['claims']} claim(s), {status['pending_proposals']} pending)")

hits = by_id["search"]["result"]["hits"]
assert hits, "search returned no hits"
print(f"search ok ({len(hits)} hit(s) for 'agent')")

ctx = by_id["context"]["result"]
assert ctx["items"], "context pack is empty"
print(f"context ok ({len(ctx['items'])} item(s) in pack)")
PY

echo
echo "== vouch JSONL quickstart example passed =="
