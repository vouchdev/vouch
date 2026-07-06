# Transports — for users

How to get vouch's `kb.*` surface in front of an agent (MCP) or a
script (JSONL).

For the framing details, see [../spec/transports.md](../spec/transports.md).
For host-specific wiring, see [../adapters/](../adapters/).

## When to use which

| You're connecting from… | Pick |
|---|---|
| Claude Code, Cursor, Codex, Continue, any MCP host | **MCP** (stdio) |
| Bash, Python script, CI job | **JSONL** |
| Another vouch instance | JSONL (today); HTTP (future) |
| A web UI you're writing | HTTP when it lands; until then, JSONL via a thin shim |

## MCP (stdio)

```bash
vouch serve                    # default
```

The server speaks MCP over stdin/stdout. Your host configures it as a
subprocess. Method `kb.search` is exposed as MCP tool `kb_search`
(dots aren't valid in MCP tool names).

### Resources

vouch also exposes read-only views as MCP resources:

| URI | content |
|---|---|
| `vouch://status` | `kb.status` result |
| `vouch://capabilities` | `kb.capabilities` |
| `vouch://pending` | pending proposals list |
| `vouch://claims/<id>` | one claim |
| `vouch://pages/<id>` | one page |

Hosts that browse resources can read those without tool calls.

### Prompts

Two named prompts you might wire up:

- `vouch.cite_this` — "register selection as evidence; propose a
  claim citing it"
- `vouch.crystallize_session` — "summarise this session's proposals
  into a page"

### How a host talks to vouch (end to end)

What actually happens when an MCP host — Claude Code, Cursor, Codex — uses
vouch over stdio:

```
host (e.g. Claude Code)                     vouch serve (child process)
       │                                              │
  1. spawn  ── command: vouch, args: [serve] ───────▶ │  one process per session;
       │      env: VOUCH_AGENT=claude-code            │  stdin/stdout pipe stays open
       │                                              │
  2. handshake  ── initialize / tools/list ─────────▶ │  (owned by the MCP SDK —
       │          ◀── kb_* tool list ─────────────────│   vouch writes no framing code)
       │                                              │
  3. tools/call kb_propose_claim ───────────────────▶ │  server.py (thin) → proposals.py
       │          ◀── result + _meta.vouch_trust ──────│  → proposed/<id>.yaml + audit event
       │                                              │
  4. tools/call kb_approve ─────────────────────────▶ │  review gate: blocks self-approval
       │          ◀── claim now durable ──────────────│  → claims/<id>.yaml + decided/<id>.yaml
```

The wire is **JSON-RPC 2.0, one message per line** over the child's
stdin/stdout — no socket, no HTTP. The host owns the subprocess; the MCP SDK
owns the `initialize → tools/list → tools/call` handshake. vouch only supplies
the `kb_*` tool implementations.

A few things worth knowing:

- **The gate is on approve, not propose.** `kb_propose_claim` writes a *pending*
  proposal under `proposed/` (gitignored); the claim becomes durable only when
  `kb_approve` runs **and** the approver isn't the proposer (unless
  `review.approver_role: trusted-agent`). Same gate on every transport.
- **Identity rides in on `VOUCH_AGENT`** (see [Identity](#identity)). The adapter
  sets it in the child's env; `claude mcp add vouch -- vouch serve` doesn't, so
  the actor falls back to `unknown-agent` — add `-e VOUCH_AGENT=…` if you want
  attribution.
- **Reads carry provenance.** Every dict-shaped result gets a `_meta.vouch_trust`
  block (`{remote, caller_kind, auth_subject}`) so the host can tell a local
  stdio call from a remote one; `kb_context` also attaches a session-gated
  `_meta.vouch_salience` sidebar.
- **vouch can talk back.** `kb_session_start` registers a server→client push
  channel, so vouch can proactively offer context during a session rather than
  only answering calls.

Tool errors come back through MCP's native error mechanism — the host surfaces
them to the model as a failed tool call, not as a `kb.*` error envelope.

## JSONL (stdin/stdout)

```bash
vouch serve --transport jsonl
```

One JSON object per line, in and out.

### Request

```json
{"id": "r1", "method": "kb.search", "params": {"query": "jwt"}}
```

### Response

```json
{"id": "r1", "ok": true, "result": [...]}
```

### Smoke test

```bash
echo '{"id":"r1","method":"kb.capabilities"}' \
  | vouch serve --transport jsonl \
  | jq '.result.methods | length'
```

A two-digit number means the server is alive.

### Pipelining

You can send N requests, then read N responses. Order matches request
order. Useful when scripting a batch:

```bash
{
  echo '{"id":"a","method":"kb.list_pending"}'
  echo '{"id":"b","method":"kb.status"}'
} | vouch serve --transport jsonl
```

## Errors

Both transports surface errors with a `code` and `message`. Codes:

- `method_not_found` — typo, or the method doesn't exist on this version
- `missing_param` — a required parameter was absent
- `invalid_request` — the envelope was malformed
- `internal_error` — unexpected; check stderr, file a bug

MCP-specific errors come through MCP's native error mechanism; the
mapping is in [../spec/transports.md](../spec/transports.md).

## Identity

Both transports respect `VOUCH_AGENT`:

```bash
VOUCH_AGENT=alice-test vouch serve --transport jsonl
```

Every audit event records that as `actor`. Use it to distinguish
agents in multi-agent setups.

## A note on auth

vouch has none. Both transports are designed for parent-process
communication — the parent is the security boundary. If you put vouch
behind a network listener, **you** are responsible for auth on top.
Don't expose `vouch serve` to the open internet.
