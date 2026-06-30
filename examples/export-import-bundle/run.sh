#!/usr/bin/env bash
set -euo pipefail

# Portable bundles: export, export-check, import-check, import-apply.
#
# move a reviewed kb between trees without trusting opaque state. export tars
# the durable kb into a portable bundle; export-check fail-closes if any file
# drifts from its manifest hash; import-check diffs a bundle against a
# destination kb without writing (new vs conflict vs identical); import-apply
# applies it with a non-destructive default policy (skip).

VOUCH="${VOUCH:-vouch}"

KB1="$(mktemp -d)"
KB2="$(mktemp -d)"
TMP="$(mktemp -d)"
BUNDLE="$TMP/bundle.tar.gz"
trap 'rm -rf "$KB1" "$KB2" "$TMP"' EXIT

hr() { printf '\n=== %s ===\n' "$1"; }

# ---------------------------------------------------------------------------
hr "build KB1 and approve two claims"
# ---------------------------------------------------------------------------
"$VOUCH" init --path "$KB1" >/dev/null
cd "$KB1"

printf 'acme-example release cadence: ships fridays.\n' > "$KB1/src1.md"
printf 'alice-example owns the deploy runbook.\n'        > "$KB1/src2.md"
S1="$("$VOUCH" source add "$KB1/src1.md" --title 'release cadence note')"
S2="$("$VOUCH" source add "$KB1/src2.md" --title 'deploy ownership note')"

# propose as one agent...
export VOUCH_AGENT=proposer-agent
P1="$("$VOUCH" propose-claim --text 'acme-example ships releases on fridays' --source "$S1")"
P2="$("$VOUCH" propose-claim --text 'alice-example owns the deploy runbook'   --source "$S2")"

# ...approve as another (vouch forbids self-approval: writes go through review).
export VOUCH_AGENT=reviewer-agent
"$VOUCH" approve "$P1" "$P2"

# ---------------------------------------------------------------------------
hr "export — tar the durable KB into a portable bundle"
# ---------------------------------------------------------------------------
# prints bundle_id (content hash of the manifest) and the file count.
"$VOUCH" export --out "$BUNDLE"

# ---------------------------------------------------------------------------
hr "export-check — fail-closed gate: every file must match its manifest hash"
# ---------------------------------------------------------------------------
# exit 0 + ok:true means another tool can trust the bundle.
"$VOUCH" export-check "$BUNDLE"

# ---------------------------------------------------------------------------
hr "build a separate destination KB2"
# ---------------------------------------------------------------------------
"$VOUCH" init --path "$KB2" >/dev/null
cd "$KB2"

# ---------------------------------------------------------------------------
hr "import-check — diff the bundle against KB2 without writing anything"
# ---------------------------------------------------------------------------
# new_files: the two imported claims + their sources (absent from KB2).
# conflicts: files present in both with different content (the seeded starter).
# identical_files: byte-identical in both (shared config) — safe to skip.
"$VOUCH" import-check "$BUNDLE"

# ---------------------------------------------------------------------------
hr "import-apply — non-destructive default (skip): never clobbers conflicts"
# ---------------------------------------------------------------------------
"$VOUCH" import-apply "$BUNDLE" --on-conflict skip

# ---------------------------------------------------------------------------
hr "status in KB2 — the imported claims are now durable"
# ---------------------------------------------------------------------------
"$VOUCH" status

printf '\nexport-import-bundle example passed\n'
