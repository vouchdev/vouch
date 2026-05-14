# JSONL shell adapter

vouch's JSONL transport lets you script the KB from bash. No MCP host
required. Useful in CI, in `make` targets, and for one-off
operations.

## Examples

### Basic round-trip

```bash
echo '{"id":"r1","method":"kb.status"}' | vouch serve --transport jsonl
```

### Register a source from stdin

```bash
cat my-rfc.md | jq -Rs '{
  id: "r1",
  method: "kb.register_source",
  params: {
    content: .,
    locator: "internal-rfc-42",
    title: "RFC 42: Caching policy",
    media_type: "text/markdown"
  }
}' | vouch serve --transport jsonl | jq -r '.result.id'
```

### List pending proposals as a table

```bash
echo '{"id":"r1","method":"kb.list_pending"}' \
  | vouch serve --transport jsonl \
  | jq -r '.result[] | [.id, .kind, .proposed_by, .proposed_at] | @tsv' \
  | column -t
```

### Approve in CI when proposal looks safe

`scripts/auto-approve.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Approve proposals from a trusted bot if they only touch existing
# entities. This is a deliberately narrow auto-approver — most teams
# should NOT auto-approve at all.

pending=$(echo '{"id":"r1","method":"kb.list_pending"}' \
  | vouch serve --transport jsonl \
  | jq -r '.result[] | select(.proposed_by == "release-notes-bot") | .id')

for id in $pending; do
  echo "{\"id\":\"r1\",\"method\":\"kb.approve\",\"params\":{\"proposal_id\":\"$id\",\"reason\":\"auto: release-notes-bot\"}}" \
    | vouch serve --transport jsonl \
    | jq -e '.ok == true' >/dev/null
done
```

**Don't ship that without thinking.** Auto-approving anything is a
foot-gun. If you do it, scope it to a specific agent and audit weekly.

## Errors

Errors come back with `ok: false`:

```bash
$ echo '{"id":"r1","method":"kb.nope"}' | vouch serve --transport jsonl
{"id":"r1","ok":false,"error":{"code":"method_not_found","message":"unknown method: kb.nope"}}
```

Test patterns:

```bash
echo '{"id":"r1","method":"kb.search","params":{"query":"jwt"}}' \
  | vouch serve --transport jsonl \
  | jq -e '.ok'
```

`jq -e` exits non-zero on `false`/`null`, so it's a clean test in CI.
