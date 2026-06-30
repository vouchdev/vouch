#!/usr/bin/env bash
set -euo pipefail

# Wire vouch into a host (install-mcp) — the operator's discovery surface.
#
# Marked a *playbook*: a real install targets a user's own project tree and the
# higher tiers (T2..T4) drop CLAUDE.md/AGENTS.md, slash commands, and host
# settings into that tree. This script only runs the SAFE, read-only legs:
#   1. install-mcp --list   (print the adapter catalogue — no writes)
#   2. a T1 install into a THROWAWAY temp project (MCP wire only, one file)
#   3. kb.capabilities + kb.status over the temp KB, to show the host can now
#      discover the surface the wire points at.
# No network, no spawned agent. See README for the full T1..T4 tier ladder.

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"        # throwaway project root that doubles as the KB root
REQUESTS="$(mktemp)"
RESPONSES="$(mktemp)"
trap 'rm -rf "$KB" "$REQUESTS" "$RESPONSES"' EXIT

export VOUCH_AGENT=example-agent

echo "== vouch install-mcp playbook =="

# 1. The adapter catalogue. Read-only: --list never touches a project tree.
echo
echo "-- install-mcp --list (adapter catalogue) --"
"$VOUCH" install-mcp --list

# 2. A fresh throwaway project. init seeds the .vouch/ KB + a starter claim.
echo
echo "-- init a throwaway project --"
"$VOUCH" init --path "$KB"

# 3. T1 install: MCP wire only. Tiers stack — T1 ⊂ T2 ⊂ T3 ⊂ T4.
#    T1 writes just the host's MCP config (.mcp.json for claude-code); it does
#    NOT touch CLAUDE.md/AGENTS.md, slash commands, or host settings.
echo
echo "-- install-mcp claude-code --tier T1 (MCP wire only) --"
"$VOUCH" install-mcp claude-code --tier T1 --path "$KB"

echo
echo "-- files the T1 wire dropped into the project --"
find "$KB" -type f -not -path '*/.vouch/*' | sed "s|$KB|<project>|" | sort

# 4. With the wire in place a host speaks kb.* over the surface. Show the two
#    discovery calls a host makes first: capabilities (method surface) and
#    status (what's in the KB). Both via the same JSONL transport the wire uses.
cd "$KB"

echo
echo "-- the host's first two discovery calls (requests.jsonl) --"
cat > "$REQUESTS" <<'JSONL'
{"id":"caps","method":"kb.capabilities","params":{}}
{"id":"status","method":"kb.status","params":{}}
JSONL
cat "$REQUESTS"

echo
echo "-- response envelopes (responses.jsonl) --"
"$VOUCH" serve --transport jsonl < "$REQUESTS" | tee "$RESPONSES"

# 5. Also show the human mirror: `vouch capabilities` is the same surface the
#    JSONL kb.capabilities returns, for an operator eyeballing the install.
echo
echo "-- vouch capabilities (human mirror, method count) --"
CAPS="$(mktemp)"
"$VOUCH" capabilities > "$CAPS"
python3 - "$CAPS" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"spec {d['spec']} (level {d['level']}) — {len(d['methods'])} kb.* methods")
PY
rm -f "$CAPS"

# 6. Assertions: the wire file landed, and both discovery calls succeeded.
echo
echo "-- assertions --"
python3 - "$RESPONSES" "$KB" <<'PY'
import json, sys
from pathlib import Path

responses, kb = sys.argv[1], sys.argv[2]

wire = Path(kb) / ".mcp.json"
assert wire.exists(), "T1 install did not write the MCP wire (.mcp.json)"
print(f"mcp wire ok ({wire.name} present)")

rows = [json.loads(line) for line in open(responses) if line.strip()]
by_id = {r["id"]: r for r in rows}

caps = by_id["caps"]
assert caps["ok"], f"kb.capabilities failed: {caps}"
methods = caps["result"]["methods"]
for m in ("kb.capabilities", "kb.status"):
    assert m in methods, f"{m} missing from capabilities.methods"
print(f"kb.capabilities ok ({len(methods)} methods discoverable)")

status = by_id["status"]
assert status["ok"], f"kb.status failed: {status}"
s = status["result"]
assert s["claims"] >= 1, s
print(f"kb.status ok ({s['claims']} claim(s), {s['pending_proposals']} pending)")
PY

echo
echo "== vouch install-mcp playbook passed =="
echo
echo "NOTE: this ran only the read-only legs. A real install targets your own"
echo "project tree; T2..T4 add CLAUDE.md/AGENTS.md, slash commands, and host"
echo "settings (see README). Run those against a project you intend to commit."
