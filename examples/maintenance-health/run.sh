#!/usr/bin/env bash
set -euo pipefail

# maintenance-health — the operator toolkit for keeping a vouch KB healthy.
#
# builds a small KB (two cited claims), then deliberately breaks one citation
# by removing the source it depends on, so the health commands have something
# real to report:
#
#   index   — rebuild state.db from the durable yaml/md files
#   lint    — surface user-actionable problems (broken citation, exit 1)
#   doctor  — full sweep: lint + source verification + index check, with counts
#   source verify — re-hash every source still on disk, report drift (ok markers)
#   stats   — pending queue, review rates, citation coverage (with broken count)
#
# also shows the JSONL transport form of the index rebuild.

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
cleanup() { rm -rf "$KB"; }
trap cleanup EXIT

export VOUCH_AGENT=example-agent

hr() { printf '\n=== %s ===\n' "$1"; }

hr "init a fresh KB"
"$VOUCH" init --path "$KB" >/dev/null
cd "$KB"

# single-agent demo: let the example agent approve its own proposals.
# in a real KB a human (or a separate trusted agent) is the approver.
python3 - "$KB" <<'PY'
import sys, pathlib, yaml
cfg = pathlib.Path(sys.argv[1], ".vouch", "config.yaml")
data = yaml.safe_load(cfg.read_text()) or {}
data.setdefault("review", {})["approver_role"] = "trusted-agent"
cfg.write_text(yaml.safe_dump(data))
PY

hr "add two sources + approve a cited claim for each"
printf 'acme-example ADR: auth uses JWT RS256.\n' > adr.md
printf 'acme-example runbook: refresh tokens rotate every 24h.\n' > runbook.md
SRC_ADR="$("$VOUCH" source add adr.md --title 'acme-example ADR')"
SRC_RUN="$("$VOUCH" source add runbook.md --title 'acme-example runbook')"

P1="$("$VOUCH" propose-claim \
  --text 'acme-example uses JWT RS256 for auth' \
  --source "$SRC_ADR" --type decision)"
"$VOUCH" approve "$P1"

P2="$("$VOUCH" propose-claim \
  --text 'acme-example rotates refresh tokens every 24h' \
  --source "$SRC_RUN" --type observation)"
"$VOUCH" approve "$P2"

hr "break one citation: delete the runbook source it depends on"
# vouch validates citations at propose time, so a dangling reference can only
# appear after the fact — e.g. a source file gets pruned out from under an
# already-approved claim. that is exactly the drift an operator hunts for.
rm -rf ".vouch/sources/$SRC_RUN"

hr "vouch index — rebuild state.db from the durable files"
"$VOUCH" index

hr "vouch lint --stale-days 30 — user-actionable findings (exits 1)"
LINT_RC=0
"$VOUCH" lint --stale-days 30 || LINT_RC=$?
echo "lint exit code: $LINT_RC"

hr "vouch doctor — lint + source verify + index check, with counts"
DOCTOR_RC=0
"$VOUCH" doctor || DOCTOR_RC=$?
echo "doctor exit code: $DOCTOR_RC"

hr "vouch source verify — re-hash every source still on disk"
# the surviving sources re-hash cleanly (stored=ok). the deleted one is simply
# gone, so it no longer appears here — its damage shows up as the broken
# citation that lint/doctor/stats report.
"$VOUCH" source verify

hr "vouch stats --json — citation coverage + broken count"
"$VOUCH" stats --json | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["citations"], indent=2, sort_keys=True))'

hr "JSONL transport: kb.index_rebuild"
# every health method has a JSONL handler; index_rebuild is the transport form
# of `vouch index`. requests are newline-delimited json on stdin.
echo '{"id":"rebuild-1","method":"kb.index_rebuild","params":{}}' | "$VOUCH" serve --transport jsonl

hr "done"
echo "temp KB cleaned up: $KB"
