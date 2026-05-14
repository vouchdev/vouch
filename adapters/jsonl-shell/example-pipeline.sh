#!/usr/bin/env bash
# Example: register a source, propose a claim citing it, list pending.
# Run from a directory containing a .vouch/ KB (or one that descends to it).

set -euo pipefail

# 1. Register a source from a file.
src_id=$(jq -Rs --arg loc "$(pwd)/example-source.md" '{
  id: "r1",
  method: "kb.register_source",
  params: {content: ., locator: $loc, title: "Example source"}
}' <<<'A short example document about caching.' \
  | vouch serve --transport jsonl \
  | jq -r '.result.id')

echo "registered source: $src_id"

# 2. Propose a claim citing that source.
prop=$(jq -n --arg src "$src_id" '{
  id: "r2",
  method: "kb.propose_claim",
  params: {
    text: "We cache GETs for 60 seconds at the CDN edge.",
    evidence: [$src],
    type: "decision",
    confidence: 0.9
  }
}' | vouch serve --transport jsonl)

echo "proposal:"
echo "$prop" | jq

# 3. List what's pending.
echo '{"id":"r3","method":"kb.list_pending"}' \
  | vouch serve --transport jsonl \
  | jq '.result[] | {id, kind, proposed_by}'
