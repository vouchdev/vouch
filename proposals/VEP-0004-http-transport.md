---
vep: "0004"
title: HTTP transport with bearer-token auth and SSE streaming for vouch serve
author: greatjourney589
status: draft
created: 2026-06-03
landed-in: ""
supersedes: []
superseded-by: ""
---

# VEP-0004: HTTP transport with bearer-token auth and SSE streaming

## Summary

Add a new `--transport http` mode to `vouch serve` that exposes the full
`kb.*` method surface over HTTP/JSON, adds a bearer-token auth layer with
per-token actor attribution, and streams audit events over Server-Sent
Events. Also adds `--transport mcp-http` for MCP over streamable-HTTP.
A companion `VOUCH_SERVER_URL` env var lets every existing CLI command
proxy through the HTTP server instead of operating on a local filesystem.

## Motivation

The two current transports — MCP stdio and JSONL stdio — require the agent
and the KB server to share a process or a pipe. This breaks down in three
common team setups:

1. **Cross-machine review.** A CI runner proposes a claim; the human
   reviewer wants to `vouch approve` it from their laptop. Today
   `proposed/` is gitignored and local-only, so this is impossible.

2. **Persistent shared sidecar.** Spawning a fresh MCP process per agent
   session means each agent gets a cold FTS5 index and must re-warm
   embeddings. A single long-lived HTTP server shares the warm index.

3. **Subprocess-hostile runtimes.** Lambda, Cloud Run, and Replit cannot
   `exec()` child processes; MCP stdio is unavailable.

## Proposal

### New CLI flags

```
vouch serve --transport http [--bind HOST:PORT] [--auth bearer|token-file|none]
vouch serve --transport mcp-http [--bind HOST:PORT] [--auth ...]
```

Defaults: `--bind 127.0.0.1:7749`, `--auth token-file`.

### HTTP wire format

```
POST /kb/<method-name>
Content-Type: application/json
Authorization: Bearer <token>

{ ...params... }

→ 200 OK  { ...result... }
→ 4xx/5xx { "error": { "code": "...", "message": "..." } }
```

Error-code → HTTP status mapping:

| vouch code        | HTTP |
|-------------------|------|
| `not_found`       | 404  |
| `validation_error`| 422  |
| `auth_error`      | 401  |
| `forbidden`       | 403  |
| `internal_error`  | 500  |

### SSE streaming

```
GET /kb/events?topics=proposals,approvals,lifecycle
Authorization: Bearer <token>

→ text/event-stream
event: proposal_created
data: {"proposal_id": "...", "actor": "ci-agent", ...}
```

### Auth modes

| mode         | behaviour |
|--------------|-----------|
| `none`       | No auth; `vouch serve` refuses to start unless `--bind` is loopback. |
| `bearer`     | Static token from `VOUCH_SERVER_TOKEN` env or `~/.vouch/server.token`. Compared with `hmac.compare_digest`. |
| `token-file` | Same as `bearer` but re-read on every request, enabling rotation without restart. |

Per-token actor attribution via `.vouch/config.yaml`:

```yaml
server:
  tokens:
    - name: ci-agent
      token_hash: sha256:<hex>
    - name: human-reviewer
      token_hash: sha256:<hex>
```

`VOUCH_AGENT` env var on the server process is the fallback actor.

### CLI proxy mode

When `VOUCH_SERVER_URL` is set, every CLI command routes through the
HTTP server instead of the local filesystem:

```
VOUCH_SERVER_URL=http://kb.internal:7749 \
VOUCH_SERVER_TOKEN=<token> \
vouch pending     # fetches from remote
vouch approve <id>  # POSTs to remote
```

### Config model additions

New `ServerConfig` and `TokenEntry` Pydantic models; new optional
`server:` key in `.vouch/config.yaml`. Old configs without it continue
to work (defaults apply).

### Capabilities changes

`kb.capabilities` gains two new fields:

```json
{
  "transports": ["mcp", "jsonl", "http-jsonl", "mcp-http"],
  "auth_modes": ["none", "bearer", "token-file"]
}
```

## Design

### New files

**`src/vouch/auth.py`** — `TokenStore`, `verify_token(token) → actor|None`,
`require_auth(request) → actor` (raises `AuthError` on failure).
Token hashes are `sha256:<hex>` stored in config; comparison uses
`hmac.compare_digest(sha256(candidate), stored_hash)`.

**`src/vouch/http_server.py`** — Starlette `Router` that:
- Mounts `POST /kb/{method}` → `_dispatch(method, params, actor)` which
  calls the same `HANDLERS` dict as `jsonl_server.py`.
- Mounts `GET /kb/events` → async SSE generator that tails
  `audit.log.jsonl` and filters by `?topics=`.
- Auth middleware that extracts the `Authorization: Bearer` header,
  calls `auth.require_auth`, and injects the actor into request state.
- Maps domain exceptions to HTTP status codes (see table above).

### Shared dispatch

`jsonl_server.py` already has `HANDLERS: dict[str, Callable[[dict], Any]]`.
`http_server.py` imports and reuses them directly — no duplication.

### SSE delivery

The SSE endpoint polls `audit.log.jsonl` with a short sleep (`asyncio.sleep(0.5)`)
and sends new lines as they appear. This is intentionally simple — no
message broker, no websocket upgrade. Topics filter by the `event` field
prefix (`proposals` → `proposal.*`, `approvals` → `proposal.approve`,
`lifecycle` → `claim.*`).

### mcp-http

`vouch serve --transport mcp-http` runs FastMCP's streamable-HTTP transport
if the MCP library supports it, falling back with a clear error if not.
This keeps the MCP tool surface intact for runtimes that speak MCP-over-HTTP.

## Compatibility

- Existing `stdio` and `jsonl` transports: **no change**.
- `vouch serve` with no flags: **no change** (still MCP stdio).
- `.vouch/config.yaml`: new optional `server:` key; old configs load fine.
- Bundle format: **unchanged**.
- `kb.capabilities` shape: additive (`transports`, `auth_modes` fields);
  old adapters that don't read these fields are unaffected.
- New optional-dependency group `[http]` in `pyproject.toml`; base
  install is unchanged.

## Security implications

**Loopback enforcement for `--auth none`:** The server refuses to bind to
a non-loopback address without an auth token. This prevents accidentally
exposing an unauthenticated KB to the network.

**Timing-safe comparison:** `hmac.compare_digest` prevents timing oracle
attacks on token comparison.

**Token hashes, not plaintext:** Config stores `sha256:<hex>` of each
token, not the token itself. An attacker who reads `config.yaml` cannot
recover the token.

**Actor injection:** The `VOUCH_AGENT` header is NOT trusted from the
client. The server resolves actor from the token's configured `name`. This
prevents clients from forging attribution.

**No new write paths outside the review gate:** HTTP handlers call the same
proposal machinery as the JSONL server. The review gate is unchanged.

## Performance implications

- SSE polling adds at most ~0.5 s latency per event; for team review
  workflows this is acceptable. A future VEP can replace with inotify/
  kqueue for sub-100 ms latency.
- Starlette + uvicorn add ~20 MB RSS; acceptable for a persistent sidecar.
- JSONL dispatch overhead is negligible (same handlers as existing transport).

## Open questions

1. Should `token-file` support multiple files (one per agent) or a single
   shared file? Current proposal: single file for simplicity; per-agent
   files are config-level anyway (via the `tokens:` list in config.yaml).
2. TLS termination: current proposal says "reverse-proxy handles TLS".
   Should we add `--tls-cert/--tls-key` as a first-class flag? Deferred
   to a follow-up VEP to keep scope small.
3. Rate limiting and request body size cap: not in this VEP. The assumption
   is the server is behind a trusted network or reverse proxy. A future VEP
   can add middleware.

## Alternatives considered

- **`socat` over TCP:** No auth, no TLS, no streaming. Fragile.
- **Git-sync proposals:** Pollutes PR history; defeats review-gate semantics.
- **Dedicated webhook server:** Solves streaming but not remote-approve.
- **MCP-over-HTTP only:** Breaks existing JSONL adapters.

## References

- Issue: feat: HTTP transport with bearer-token auth and SSE streaming
- VEP-0002: JSONL transport (the transport this extends)
- MCP streamable-HTTP spec
- AKBP §7: transport negotiation
