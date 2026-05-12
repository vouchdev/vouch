# Transports

A vouch server speaks one or more of the transports listed in its
`kb.capabilities.transports` array. This document defines both.

The *method surface* is identical across transports — only framing
differs. Whichever transport you implement, the parameter and result
shapes are those in [methods.md](methods.md).

---

## 1. MCP over stdio

The default transport. The server is invoked as a subprocess by an MCP
host (Claude Code, Cursor, Codex, …) over stdin/stdout following the
[Model Context Protocol][mcp] specification.

[mcp]: https://modelcontextprotocol.io

### Tool naming

MCP tool names cannot contain dots, so `kb.search` is exposed as the
MCP tool **`kb_search`**. The mapping is mechanical: replace `.` with
`_`. The dotted name remains canonical in `kb.capabilities.methods`.

### Resources

A vouch MCP server SHOULD expose the KB's read-only views as MCP
resources under the `vouch://` scheme:

- `vouch://status` — JSON, same shape as `kb.status`
- `vouch://capabilities` — JSON
- `vouch://pending` — JSON list of pending proposals
- `vouch://claims/<id>` — claim YAML
- `vouch://pages/<id>` — page markdown

Hosts that prefer resource browsing over tool calling can read those
directly.

### Prompts

A vouch MCP server MAY expose a small set of named prompts to make
common agent tasks one-click:

- `vouch.cite_this` — "register the current selection as evidence and
  propose a claim citing it"
- `vouch.crystallize_session` — "summarize this session's proposals
  into a session page"

Prompts are sugar; they all decompose into the `kb.*` tool calls.

---

## 2. JSONL over stdin/stdout

A simpler transport for setups without MCP. One JSON object per line,
newline-delimited.

### Request envelope

```json
{"id": "r1", "method": "kb.search", "params": {"query": "jwt", "limit": 5}}
```

- `id` *(string, required)* — opaque correlation id; echoed in the
  response.
- `method` *(string, required)* — dotted name.
- `params` *(object, optional)* — method-specific.

Extra keys are ignored. Requests without `id` or `method` get an
`invalid_request` error.

### Response envelope (success)

```json
{"id": "r1", "ok": true, "result": [...]}
```

### Response envelope (error)

```json
{"id": "r1", "ok": false, "error": {"code": "missing_param", "message": "query is required"}}
```

### Error codes

| code | meaning |
|---|---|
| `method_not_found` | unknown `method` |
| `missing_param` | required param absent or `null` |
| `invalid_request` | malformed envelope (no `id` / no `method`, non-object `params`, etc.) |
| `internal_error` | unexpected server-side failure; include `message` |

Implementations MAY add other codes prefixed with their name
(e.g. `vouch_forbidden_self_approval`). Standard clients SHOULD treat
unknown codes as errors.

### Framing rules

- One request per line. Trailing newline is required.
- Server response order MUST match the request `id`, but requests MAY
  be pipelined: a client can send N requests before reading any
  responses. A conforming server processes them in order.
- `\r\n` and `\n` are both accepted as line terminators on input.
  Responses MUST use `\n`.

### Capability discovery

The first thing a JSONL client SHOULD send is:

```json
{"id": "boot", "method": "kb.capabilities"}
```

… and use the returned `methods` array to decide what's available
before issuing other calls.

---

## 3. Choosing a transport

| | MCP stdio | JSONL |
|---|---|---|
| Wired by an LLM host | yes (Claude Code, Cursor, Codex) | no — bring your own client |
| Resources / prompts | yes | no |
| Spec dependency | full MCP | none |
| Pipelining | per MCP semantics | yes |
| Test ergonomics | requires MCP host | trivial — `echo` + `jq` |

Use MCP when the consumer is an LLM agent inside a host. Use JSONL
when the consumer is a script, a CI job, or another server.

---

## 4. Future transports (non-normative)

HTTP is on the roadmap (see [ROADMAP.md](../ROADMAP.md), 0.1). The
intended shape is one POST per call against `/rpc` with the JSONL
envelope as request body and response body. Localhost-bound by default;
auth story is `Authorization: Bearer <token>` from `config.yaml`.

This isn't part of the current spec — it's a heads-up so implementers
don't accidentally choose conflicting conventions in the meantime.
