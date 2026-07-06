#!/usr/bin/env bash
#
# End-to-end smoke test for auto-capture (docs/superpowers/specs/
# 2026-07-01-vouch-session-autocapture-design.md).
#
# Drives the real `vouch` CLI through the three hook payloads Claude Code would
# send — PostToolUse (observe), SessionEnd (finalize), SessionStart (banner) —
# in a throwaway KB, and asserts the review-gated outcome. No network, no LLM.
#
# Usage:
#   scripts/smoke-capture.sh                       # uses `vouch` on PATH
#   VOUCH=.venv/bin/vouch scripts/smoke-capture.sh # a specific binary
#   VOUCH="python -m vouch" scripts/smoke-capture.sh
#   make smoke-capture
#
# Exits 0 if every check passes, 1 otherwise.

set -uo pipefail

VOUCH="${VOUCH:-vouch}"
# Absolutize a path-form first token (e.g. .venv/bin/vouch) so the checks work
# no matter the caller's cwd. A bare name on PATH or a module form
# ("python -m vouch") is left as-is; multi-word commands run via `V()` below.
set -- $VOUCH
_first="$1"; shift
case "$_first" in
  */*) _first="$(cd "$(dirname "$_first")" && pwd)/$(basename "$_first")" ;;
esac
VOUCH="$_first${*:+ $*}"

# Run the vouch CLI (unquoted expansion supports a multi-word command).
V() { $VOUCH "$@"; }

PASS=0
FAIL=0
green() { printf '\033[32m%s\033[0m\n' "$1"; }
red() { printf '\033[31m%s\033[0m\n' "$1"; }

check() {  # check "description" "expected" "actual"
  if [ "$2" = "$3" ]; then
    green "PASS  $1"; PASS=$((PASS + 1))
  else
    red "FAIL  $1"; red "        expected: $2"; red "        actual:   $3"; FAIL=$((FAIL + 1))
  fi
}

contains() {  # contains "description" "haystack" "needle"
  case "$2" in
    *"$3"*) green "PASS  $1"; PASS=$((PASS + 1)) ;;
    *) red "FAIL  $1 (missing: $3)"; FAIL=$((FAIL + 1)) ;;
  esac
}

if ! V --version >/dev/null 2>&1; then
  red "cannot run vouch (set VOUCH=/path/to/vouch or 'python -m vouch'). tried: $VOUCH"
  exit 1
fi

WORK="$(mktemp -d)"
WORK2="$(mktemp -d)"
cleanup() { rm -rf "$WORK" "$WORK2"; }
trap cleanup EXIT

SID="cc-smoke-$$"
echo "workspace: $WORK"
echo "session:   $SID"
echo

# --- init a throwaway KB (no cd needed: --path + VOUCH_KB_PATH) -------------
V init --path "$WORK" >/dev/null 2>&1
export VOUCH_KB_PATH="$WORK/.vouch"
check "vouch init created .vouch/" "yes" "$([ -d "$WORK/.vouch" ] && echo yes || echo no)"
contains "captures/ is gitignored" "$(cat "$WORK/.vouch/.gitignore")" "captures/"

# --- PostToolUse x3 (observe) ---------------------------------------------
obs() { echo "$1" | V capture observe; }
obs '{"session_id":"'"$SID"'","tool_name":"Edit","tool_input":{"file_path":"/p/auth.py"},"tool_response":"ok"}'
obs '{"session_id":"'"$SID"'","tool_name":"Bash","tool_input":{"command":"pytest -q"},"tool_response":"1 failed, error"}'
obs '{"session_id":"'"$SID"'","tool_name":"Write","tool_input":{"file_path":"/p/README.md"},"tool_response":"done"}'

BUF="$WORK/.vouch/captures/$SID.jsonl"
check "buffer has 3 observations" "3" "$([ -f "$BUF" ] && wc -l < "$BUF" | tr -d ' ' || echo 0)"
contains "observation summarizes the edit" "$(cat "$BUF" 2>/dev/null)" "Edited auth.py"
contains "failed command is flagged" "$(cat "$BUF" 2>/dev/null)" "Command failed"

# an unobserved (mcp) tool must NOT be captured
obs '{"session_id":"'"$SID"'","tool_name":"mcp__vouch__kb_search","tool_input":{},"tool_response":"x"}'
check "mcp tool is ignored (still 3 lines)" "3" "$(wc -l < "$BUF" | tr -d ' ')"

# garbage stdin must never error (a hook must not break the tool call)
echo 'not json' | V capture observe; rc=$?
check "observe survives garbage stdin (exit 0)" "0" "$rc"

# --- SessionEnd (finalize) -------------------------------------------------
FIN="$(echo '{"session_id":"'"$SID"'","cwd":"'"$WORK"'"}' | V capture finalize)"
contains "finalize reports captured:3" "$FIN" '"captured": 3'
contains "finalize returns a summary_proposal_id" "$FIN" '"summary_proposal_id":'
check "buffer file removed after finalize" "gone" "$([ -f "$BUF" ] && echo present || echo gone)"

# --- review gate: PENDING, authored by vouch-capture, not auto-approved -----
PEND="$(V pending 2>/dev/null)"
contains "summary is in the pending queue" "$PEND" "by vouch-capture"
contains "summary is a page proposal" "$PEND" "[page]"

# our summary (body carries the session id) must live in proposed/, not pages/.
# (vouch init seeds an unrelated starter page, so we key on our session id.)
OUR_APPROVED="$(grep -rl "$SID" "$WORK/.vouch/pages" 2>/dev/null | wc -l | tr -d ' ')"
check "our summary is NOT auto-approved (review gate intact)" "0" "$OUR_APPROVED"
N_PROPOSED="$(find "$WORK/.vouch/proposed" -name '*.yaml' 2>/dev/null | wc -l | tr -d ' ')"
check "summary sits in proposed/ awaiting review" "1" "$N_PROPOSED"

# --- SessionStart (banner) -------------------------------------------------
BANNER="$(V capture banner)"
contains "banner nudges to review" "$BANNER" "awaiting review"

# --- disabled mode is a no-op ---------------------------------------------
V init --path "$WORK2" >/dev/null 2>&1
printf 'capture:\n  enabled: false\n' >> "$WORK2/.vouch/config.yaml"
export VOUCH_KB_PATH="$WORK2/.vouch"
echo '{"session_id":"off","tool_name":"Edit","tool_input":{"file_path":"/p/x.py"},"tool_response":"ok"}' \
  | V capture observe
check "capture.enabled:false writes no buffer" "gone" \
  "$([ -f "$WORK2/.vouch/captures/off.jsonl" ] && echo present || echo gone)"

# --- report ----------------------------------------------------------------
echo
echo "-----------------------------------------"
if [ "$FAIL" -eq 0 ]; then
  green "ALL $PASS CHECKS PASSED"; exit 0
else
  red "$FAIL FAILED, $PASS passed"; exit 1
fi
