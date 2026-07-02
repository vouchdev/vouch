#!/usr/bin/env bash
#
# End-to-end smoke test for session-start recall (docs/superpowers/specs/
# 2026-07-01-vouch-session-autocapture-design.md).
#
# Seeds a throwaway KB with approved knowledge via the real CLI, then checks
# that `vouch recall` — the command the SessionStart hook runs — emits a digest
# a new Claude session can consume: every live approved claim + page title,
# archived claims excluded, opt-out honoured. No network, no LLM.
#
# Usage:
#   scripts/smoke-recall.sh                       # uses `vouch` on PATH
#   VOUCH=.venv/bin/vouch scripts/smoke-recall.sh
#   make smoke-recall
#
# Exits 0 if every check passes, 1 otherwise.

set -uo pipefail

VOUCH="${VOUCH:-vouch}"
set -- $VOUCH
_first="$1"; shift
case "$_first" in
  */*) _first="$(cd "$(dirname "$_first")" && pwd)/$(basename "$_first")" ;;
esac
VOUCH="$_first${*:+ $*}"
V() { $VOUCH "$@"; }

PASS=0
FAIL=0
green() { printf '\033[32m%s\033[0m\n' "$1"; }
red() { printf '\033[31m%s\033[0m\n' "$1"; }

check() {  # check "desc" "expected" "actual"
  if [ "$2" = "$3" ]; then green "PASS  $1"; PASS=$((PASS + 1))
  else red "FAIL  $1"; red "        expected: $2"; red "        actual:   $3"; FAIL=$((FAIL + 1)); fi
}
contains() {  # contains "desc" "haystack" "needle"
  case "$2" in *"$3"*) green "PASS  $1"; PASS=$((PASS + 1)) ;;
    *) red "FAIL  $1 (missing: $3)"; FAIL=$((FAIL + 1)) ;; esac
}
absent() {  # absent "desc" "haystack" "needle"
  case "$2" in *"$3"*) red "FAIL  $1 (should be absent: $3)"; FAIL=$((FAIL + 1)) ;;
    *) green "PASS  $1"; PASS=$((PASS + 1)) ;; esac
}

if ! V --version >/dev/null 2>&1; then
  red "cannot run vouch (set VOUCH=/path/to/vouch or 'python -m vouch'). tried: $VOUCH"
  exit 1
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

V init --path "$WORK" >/dev/null 2>&1
export VOUCH_KB_PATH="$WORK/.vouch"

# --- seed approved knowledge via the real CLI ------------------------------
printf 'team decision doc\n' > "$WORK/evidence.txt"
SRC="$(V source add "$WORK/evidence.txt")"
check "registered a source" "yes" "$([ -n "$SRC" ] && echo yes || echo no)"

# propose as an agent, approve as the human — self-approval is forbidden.
approve_claim() {  # approve_claim "text" -> prints claim id
  local prop cid
  prop="$(VOUCH_AGENT=claude-code V propose-claim --text "$1" --source "$SRC" 2>/dev/null)"
  cid="$(V approve "$prop" 2>/dev/null | sed -n 's#.*claim/##p')"
  echo "$cid"
}

approve_claim "use ruff not flake8 for linting" >/dev/null
approve_claim "jwt over sessions for the microservices auth" >/dev/null
ARCH_ID="$(approve_claim "ephemeral fact to be archived")"

PROP_PAGE="$(VOUCH_AGENT=claude-code V propose-page --title "auth design record" --body "the why behind auth" 2>/dev/null)"
V approve "$PROP_PAGE" >/dev/null 2>&1

# archive one claim — it must drop out of the digest
V archive "$ARCH_ID" >/dev/null 2>&1

# --- what a new session gets injected --------------------------------------
DIGEST="$(V recall)"
contains "digest is wrapped in the injection tag" "$DIGEST" "<vouch-approved-knowledge>"
contains "digest lists an approved claim" "$DIGEST" "use ruff not flake8 for linting"
contains "digest lists the second approved claim" "$DIGEST" "jwt over sessions for the microservices auth"
contains "digest lists the approved page title" "$DIGEST" "auth design record"
absent "archived claim is excluded" "$DIGEST" "ephemeral fact to be archived"

# --- opt-out ---------------------------------------------------------------
printf 'recall:\n  enabled: false\n' >> "$WORK/.vouch/config.yaml"
OFF="$(V recall)"
check "recall.enabled:false emits nothing" "" "$(printf '%s' "$OFF" | tr -d '[:space:]')"

# --- adapter wiring --------------------------------------------------------
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOKED="$(python3 -c "import json;h=json.load(open('$REPO/adapters/claude-code/.claude/settings.json'))['hooks']['SessionStart'];print(any('vouch recall' in c.get('command','') for g in h for c in g['hooks']))" 2>/dev/null)"
check "SessionStart hook runs 'vouch recall'" "True" "$HOOKED"

# --- report ----------------------------------------------------------------
echo
echo "-----------------------------------------"
if [ "$FAIL" -eq 0 ]; then green "ALL $PASS CHECKS PASSED"; exit 0
else red "$FAIL FAILED, $PASS passed"; exit 1; fi
