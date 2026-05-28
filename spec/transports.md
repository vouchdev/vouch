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

## 3. HTTP

`vouch serve --transport http` exposes the same `kb.*` surface over a
long-lived HTTP server, so several clients can share one KB without each
spawning a subprocess. The method surface is identical — only framing
differs.

### Endpoints

| Method & path        | Auth | Body                                                |
|----------------------|------|-----------------------------------------------------|
| `POST /rpc`          | yes* | JSONL envelope (§2) as request body **and** response body |
| `GET /capabilities`  | no   | `kb.capabilities` JSON                              |
| `GET /healthz`       | no   | `{"ok": true}` liveness probe                       |

\* `/rpc` requires a bearer token only when one is configured (see Auth).

```bash
curl -s localhost:8731/rpc \
  -d '{"id":"r1","method":"kb.search","params":{"query":"jwt"}}'
# {"id":"r1","ok":true,"result":{"backend":"...","hits":[...]}}
```

### Bind policy

Binds `127.0.0.1` by default. The server **refuses to bind a non-loopback
host** unless both `--allow-public` and a token are supplied — so an
unauthenticated KB can never be exposed to the network by accident.

### Auth

When a token is set (`--token` or `VOUCH_HTTP_TOKEN`), every `POST /rpc`
must send `Authorization: Bearer <token>`; the comparison is
constant-time. `GET /capabilities` and `/healthz` are always
unauthenticated (they leak only the method list and liveness). There is
no in-process TLS — terminate TLS at a reverse proxy for public
deployments. The review gate is unchanged: HTTP clients file proposals
exactly like every other transport, and `kb.approve` over HTTP is the
same privileged operation it is everywhere.

### Actor attribution

The `X-Vouch-Agent` request header maps to the audit `actor` for that
request (mirroring `VOUCH_AGENT` for stdio/JSONL). Absent header →
`unknown-agent`.

---

## 4. Choosing a transport

| | MCP stdio | JSONL | HTTP |
|---|---|---|---|
| Wired by an LLM host | yes (Claude Code, Cursor, Codex) | no | no |
| Resources / prompts | yes | no | no |
| Multiple clients, one process | no | no | yes |
| Spec dependency | full MCP | none | none (plain HTTP) |
| Test ergonomics | requires MCP host | trivial — `echo` + `jq` | trivial — `curl` |

Use MCP when the consumer is an LLM agent inside a host. Use JSONL when
the consumer is a local script or CI job. Use HTTP when several clients
(or a remote/self-hosted deployment) need to share one KB.

---

## 5. Future transports (non-normative)

An MCP **streamable-HTTP** transport (as opposed to the bespoke
JSONL-over-HTTP above) may follow if a hosted MCP client needs it; see
[VEP-0004](../proposals/VEP-0004-http-transport.md). Not part of the
current spec — a heads-up so implementers don't pick conflicting
conventions in the meantime.
