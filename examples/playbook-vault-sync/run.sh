#!/usr/bin/env bash
set -euo pipefail

# playbook-vault-sync — one non-watch backward+forward cycle between a vouch
# KB and an Obsidian-style markdown vault. The review gate stays intact: a
# vault edit becomes a page-edit *proposal*, never a silent write.
#
# Override the binary with: VOUCH=/path/to/vouch bash run.sh
VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)/proj"
VAULT="$(mktemp -d)/vault"
trap 'rm -rf "$KB" "$VAULT"' EXIT

export VOUCH_AGENT=example-agent

echo "=============================================================="
echo " 1. init a fresh KB (seeds one approved page + one claim)"
echo "=============================================================="
"$VOUCH" init --path "$KB" | sed 's/^/    /'
cd "$KB"

echo
echo "=============================================================="
echo " 2. backward sync (KB -> vault): mirror approved pages/claims"
echo "=============================================================="
mkdir -p "$VAULT"
"$VOUCH" sync --vault "$VAULT" --direction backward | sed 's/^/    /'
echo "    vault tree:"
find "$VAULT/vouch" -type f | sort | sed "s#$VAULT/#      #"

echo
echo "=============================================================="
echo " 3. edit a mirrored page file (simulating an Obsidian edit)"
echo "=============================================================="
printf '\n\nNew paragraph added in the vault by alice-example.\n' \
    >> "$VAULT/vouch/pages/edit-in-obsidian.md"
echo "    appended a paragraph to vouch/pages/edit-in-obsidian.md"

echo
echo "=============================================================="
echo " 4. forward sync (vault -> KB): the edit becomes a PROPOSAL"
echo "    (it cites a vault:<relpath> source, so the gate sees the"
echo "     exact bytes — nothing is written to the KB yet)"
echo "=============================================================="
"$VOUCH" sync --vault "$VAULT" --direction forward | sed 's/^/    /'

echo
echo "=============================================================="
echo " 5. the page edit is sitting in the review queue, not applied"
echo "=============================================================="
"$VOUCH" pending | sed 's/^/    /'

echo
echo "=============================================================="
echo " 6. JSONL transport: list the pending proposal + read the"
echo "    still-approved original page (its body is UNCHANGED — the"
echo "    review gate held)"
echo "=============================================================="
printf '%s\n%s\n' \
  '{"id":"r1","method":"kb.list_pending","params":{}}' \
  '{"id":"r2","method":"kb.read_page","params":{"page_id":"edit-in-obsidian"}}' \
  | "$VOUCH" serve --transport jsonl 2>/dev/null \
  | sed 's/^/    /'

echo
echo "=============================================================="
echo " 7. --watch is a LONG-LIVED loop (playbook step — NOT auto-run)"
echo "=============================================================="
cat <<EOF | sed 's/^/    /'
This example ran a single non-watch cycle. In day-to-day use you keep a
polling loop alive so vault edits flow into the review queue continuously:

    vouch sync --vault ~/Obsidian/YourVault --watch --poll 2

It blocks until Ctrl-C, re-running forward+backward every --poll seconds.
We do not auto-run it here because it never exits on its own.
EOF

echo
echo "playbook-vault-sync: done."
