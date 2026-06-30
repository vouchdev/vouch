#!/usr/bin/env bash
set -euo pipefail

# context-and-synthesize — two retrieval surfaces an agent injects into a
# prompt. kb.context builds a budgeted ContextPack (with a require-citations
# quality gate); kb.synthesize answers a question from approved claims only,
# with inline [cite] markers; kb.search shows the raw hits the pack drew from.
#
# Override the binary with VOUCH=/path/to/vouch (the example verifies against
# the real 1.0.0 build).

VOUCH="${VOUCH:-vouch}"
export VOUCH_AGENT=example-agent

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

section() { printf '\n=== %s ===\n' "$1"; }

section "init a fresh KB"
"$VOUCH" init --path "$KB" >/dev/null
cd "$KB"
echo "kb at $KB"

# ---------------------------------------------------------------------------
# Seed: register one source file, then propose four cited claims about auth.
# Claims are proposed as alice-example and approved as reviewer-example —
# vouch forbids self-approval, which is the whole point of the review gate.
# ---------------------------------------------------------------------------
mkdir -p docs
cat > docs/auth-rfc.md <<'DOC'
# acme-example auth design (RFC)

- access tokens are JSON Web Tokens signed with RS256
- access tokens live 15 minutes, then must be refreshed
- refresh tokens rotate on every use; the old one is revoked
- all auth endpoints are rate-limited to 10 requests/minute per ip
DOC

section "register a source"
SID="$(VOUCH_AGENT=alice-example "$VOUCH" source add docs/auth-rfc.md \
  --title "acme-example auth RFC" --type file)"
echo "source id: $SID"

propose_and_approve() {
  local text="$1" conf="$2"
  local pid
  pid="$(VOUCH_AGENT=alice-example "$VOUCH" propose-claim \
    --text "$text" --source "$SID" --type fact --confidence "$conf")"
  VOUCH_AGENT=reviewer-example "$VOUCH" approve "$pid" >/dev/null
  echo "approved: $text"
}

section "propose + approve four cited claims (review gate enforced)"
propose_and_approve "Authentication uses short-lived JWT access tokens signed with RS256." 0.95
propose_and_approve "JWT access tokens expire after 15 minutes and must be refreshed." 0.9
propose_and_approve "Refresh tokens rotate on every use; the previous token is revoked." 0.9
propose_and_approve "Auth endpoints are rate-limited to 10 requests per minute per ip." 0.8

# ---------------------------------------------------------------------------
# kb.search — the raw retrieval the pack draws from.
# ---------------------------------------------------------------------------
section "kb.search: raw hits"
"$VOUCH" search "jwt token" --json | python3 -c '
import json, sys
res = json.load(sys.stdin)
hits = res.get("hits", [])
print(str(len(hits)) + " hits (backend: " + str(res.get("backend")) + ")")
for h in hits[:5]:
    text = h.get("snippet") or h.get("text") or h.get("title") or ""
    print("  - [" + str(h.get("score")) + "] " + text[:64])
'

# ---------------------------------------------------------------------------
# kb.context — a budgeted ContextPack. --require-citations + --min-items make
# it a quality gate: it refuses to emit an uncited / under-populated pack.
# ---------------------------------------------------------------------------
section "kb.context: budgeted ContextPack with require-citations gate"
"$VOUCH" context "jwt token" \
  --limit 5 --max-chars 2000 --require-citations --min-items 1 \
  | python3 -c '
import json, sys
pack = json.load(sys.stdin)
items = pack.get("items", [])
q = pack.get("quality", {})
print("items in pack: " + str(len(items)))
print("budget (max-chars 2000): clipped=" + str(q.get("budget_clipped_items"))
      + " omitted=" + str(q.get("budget_omitted_items"))
      + " truncated=" + str(q.get("budget_truncated")))
print("quality gate ok=" + str(q.get("ok"))
      + " require_citations=" + str(q.get("require_citations"))
      + " min_items=" + str(q.get("minimum_items")))
for it in items:
    cites = it.get("citations") or it.get("evidence") or []
    print("  - " + (it.get("summary") or it.get("text") or "")[:64])
    print("      citations: " + str(cites))
'

# ---------------------------------------------------------------------------
# The gate in action: a query with no cited hits yields an empty pack whose
# quality.ok is false. The agent inspects quality.ok before injecting — an
# uncited / under-populated pack is refused rather than silently used.
# ---------------------------------------------------------------------------
section "kb.context: gate flags an uncited / empty pack (quality.ok=false)"
"$VOUCH" context "billing" \
  --limit 5 --require-citations --min-items 1 \
  | python3 -c '
import json, sys
pack = json.load(sys.stdin)
q = pack.get("quality", {})
print("items: " + str(len(pack.get("items", []))))
print("quality.ok: " + str(q.get("ok")) + "  (failed checks: " + str(q.get("failed")) + ")")
print("warnings: " + str(pack.get("warnings")))
if q.get("ok"):
    sys.exit("expected gate to refuse this pack")
print("agent declines to inject: gate held.")
'

# ---------------------------------------------------------------------------
# kb.synthesize — a cited answer assembled from approved claims only.
# ---------------------------------------------------------------------------
section "kb.synthesize: cited answer (inline markers)"
"$VOUCH" synthesize "jwt token" --depth 5 --max-chars 2000 \
  | python3 -c '
import json, sys
res = json.load(sys.stdin)
print(res.get("answer") or "(no answer)")
claims = res.get("claims") or []
gaps = res.get("gaps") or []
print("\nclaims cited: " + str(len(claims)) + "  gaps: " + str(gaps))
'

section "done"
echo "all four surfaces returned cited, review-gated knowledge."
