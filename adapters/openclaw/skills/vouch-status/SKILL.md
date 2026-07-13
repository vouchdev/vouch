---
name: vouch-status
description: Show the project's vouch KB at a glance
---

# /vouch-status

Run vouch's `kb_status` MCP tool and surface the result. Use this when the
user asks "what's in our KB?" or before/after a long claim-proposal flow so
they can see the proposal count tick up.

Format:

```
KB at <root>
  claims:     <n>
  sources:    <n>
  entities:   <n>
  pending:    <n>   ← review queue depth
  last audit: <iso8601 timestamp>
```

If `pending > 0`, suggest the user run `vouch approve <id>` (or `vouch lint`
if they want to inspect anything first). Do not propose anything yourself.
