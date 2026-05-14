# Generic MCP adapter

Reference for hosts that speak MCP but aren't in the list of named
adapters.

## Command

```
vouch serve
```

stdio transport. Discovers `.vouch/` by walking up from the cwd.

## Recommended env

| var | purpose | example |
|---|---|---|
| `VOUCH_AGENT` | identity for `proposed_by` and audit-log actor | `my-host`, `acme-bot-7` |
| `VOUCH_KB_PATH` | force a specific KB root (skip discovery) | `/abs/path/.vouch` |
| `VOUCH_LOG_FORMAT` | `text` (default) or `json` | `json` |

## Tool naming

MCP tool names cannot contain dots. Methods are exposed with `_`:

| canonical | MCP tool name |
|---|---|
| `kb.search` | `kb_search` |
| `kb.propose_claim` | `kb_propose_claim` |
| `kb.approve` | `kb_approve` |
| … | … |

Full list: call `kb_capabilities` and read the `methods` array (those
are the canonical names; convert dots to underscores).

## Resources

vouch exposes:

| URI | content |
|---|---|
| `vouch://status` | `kb.status` result as JSON |
| `vouch://capabilities` | `kb.capabilities` as JSON |
| `vouch://pending` | array of pending proposals |
| `vouch://claims/<id>` | claim YAML |
| `vouch://pages/<id>` | page markdown |

Hosts that prefer resource browsing over tool calling can list and
read these directly.

## Lifecycle

vouch is a long-lived stdio server. Open it on host startup, close it
on shutdown. There's no shutdown handshake — closing stdin is enough.

## Validation

Tool-call arguments are validated server-side. Hosts don't need to
duplicate validation, but they SHOULD echo back the `errors` array if
the result includes one — it's how users discover what they missed
(e.g. "claim needs at least one citation").
