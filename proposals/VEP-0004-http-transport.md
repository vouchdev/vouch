---
vep: 0004
title: HTTP transport
author: dripsmvcp
status: accepted
created: 2026-05-26
landed-in: "0.2.0"
supersedes: []
superseded-by: ""
---

# VEP-0004: HTTP transport

> **Status (2026-06-09):** Accepted. Initial vouch-native shape shipped in
> [PR #104](https://github.com/vouchdev/vouch/pull/104). Spec-compliant
> upgrade — MCP-over-Streamable-HTTP, multi-token accept-list, config.yaml
> `serve:` section, `adapters/http-tunnel/` reference deployment — added by
> the PR that closes [#176](https://github.com/vouchdev/vouch/issues/176).

## Summary

Add an HTTP transport (`vouch serve --transport http`) alongside the
existing stdio (MCP) and JSONL transports. Same `kb.*` method surface;
one long-lived process that multiple clients can connect to over the
network instead of each spawning a local subprocess. Binds `127.0.0.1`
by default; a bearer token is required before it will bind any
non-loopback address.

The accepted shape exposes **three protocols against the same surface**:
the MCP-over-Streamable-HTTP standard (`POST /mcp` and `/messages` alias)
that Claude.ai Custom Connectors, Claude mobile (write), Anthropic Managed
Agents, the Messages-API `mcp_servers` field, and Computer Use all speak;
the vouch-native JSONL envelope at `POST /rpc` for lightweight scripted
clients; and unauthenticated `/healthz`, `/health`, `/capabilities` probes
for monitoring + connector validation.

## Motivation

`vouch serve` today is stdio-only (MCP over stdin/stdout) or JSONL over
stdin/stdout. Both require the client to *be* the parent process — every
consumer launches its own `vouch` subprocess and owns its lifetime. That
is the right model for a single LLM host on one machine, and the wrong
model for:

- **Multiple clients sharing one KB.** Two editors, a CI job, and a
  dashboard all want the same `.vouch/`. Today that's four subprocesses,
  each re-opening `state.db`.
- **A hosted / self-hosted deployment.** Issue #94 wants a remote option
  so a team (and a future Claude plugin) can point at a URL instead of
  shipping the CLI to every machine.
- **Anything that already speaks HTTP.** A `curl` one-liner or a serverless
  function shouldn't need a pseudo-tty and a subprocess just to call
  `kb.search`.

ROADMAP 0.1 lists this and marks it **[VEP]**; VEP-0002 (JSONL transport)
explicitly deferred HTTP to "0.1 ... adds bind/auth concerns. Real plan
for 0.1." This is that plan.

## Proposal

Add `http` to the `--transport` choice on `vouch serve`, plus binding and
auth options:

```
vouch serve --transport http
            [--host 127.0.0.1]      # default loopback
            [--port 8731]
            [--token <secret>]      # or env VOUCH_HTTP_TOKEN
            [--allow-public]        # required to bind a non-loopback host
```

The method surface is unchanged: every `kb.*` method already reachable
over MCP and JSONL is reachable over HTTP, with the **same parameter and
result shapes** defined in [spec/methods.md](../spec/methods.md). No new
methods, no renamed methods, no changed parameter shapes.

### Endpoints (final accepted shape)

| Method & path        | Body / result                                              | Auth |
|----------------------|------------------------------------------------------------|------|
| `POST /mcp`          | MCP-over-Streamable-HTTP (JSON-RPC 2.0; `initialize`, `tools/list`, `tools/call`) | bearer |
| `POST /messages`     | alias for `/mcp` — historical Claude surfaces probe this path | bearer |
| `POST /rpc`          | JSONL envelope in, JSONL envelope out (identical to VEP-0002) | bearer |
| `GET /capabilities`  | `kb.capabilities` JSON (advertises the surface)            | open |
| `GET /healthz`       | `{"ok": true}` liveness probe                              | open |
| `GET /health`        | alias for `/healthz` — Claude.ai connector validator       | open |

`POST /rpc` is the whole surface. Request and response envelopes are
byte-for-byte the JSONL envelopes from VEP-0002:

```json
// request
{"id": "r1", "method": "kb.search", "params": {"query": "jwt"}}
// response
{"id": "r1", "ok": true, "result": {...}}
{"id": "r1", "ok": false, "error": {"code": "missing_param", "message": "..."}}
```

`kb.capabilities.transports` gains `"http"` when the HTTP server is the
one answering (the array reflects *reachable* transports).

## Design

The MCP and JSONL servers already route through a single internal
dispatch table (VEP-0002: `_handle_request(method, params, actor) ->
Result | Error`). The HTTP transport is a third front-end over that same
function — no business logic is duplicated.

```
src/vouch/http_server.py
  run_http(host, port, *, token, allow_public) -> None
    - refuse to start if host is non-loopback and token is None
      (unless --allow-public AND token both set)
    - http.server.ThreadingHTTPServer + a BaseHTTPRequestHandler
    - POST /rpc:
        auth check (see below) ->
        body = json.loads(rfile.read(Content-Length)) ->
        _handle_request(body["method"], body.get("params", {}), actor) ->
        write {id, ok, result|error}
    - GET /capabilities, GET /healthz: no auth
```

- **Zero new runtime dependencies.** Uses the stdlib `http.server`
  (`ThreadingHTTPServer`). No Flask/FastAPI/uvicorn. (Open question below
  on whether to adopt the MCP streamable-HTTP transport instead.)
- **Actor attribution.** The `X-Vouch-Agent` request header maps to the
  audit `actor`, mirroring how `VOUCH_AGENT` works for stdio/JSONL. Absent
  header → `unknown-agent`, exactly as the other transports default.
- **Concurrency.** `ThreadingHTTPServer` serves requests on threads.
  Writes already go through the file-backed store with exclusive-create
  semantics and SQLite's own locking; the review gate is unchanged. We
  document that a single KB behind multiple writers relies on those
  existing guarantees and add a smoke test for concurrent `kb.search`.
- **Wiring.** `cli.py serve` gains the `http` branch:
  `from .http_server import run_http; run_http(host, port, token=..., allow_public=...)`.

### Auth model

| Bind                         | Token required? | Rationale                                  |
|------------------------------|-----------------|--------------------------------------------|
| `127.0.0.1` / `::1` (default)| No              | Same trust boundary as JSONL: same machine |
| any non-loopback             | **Yes**         | Refuse to start without `--allow-public` + a token |

When a token is configured, every `POST /rpc` must send
`Authorization: Bearer <token>`; comparison is constant-time
(`hmac.compare_digest`). `GET /capabilities` and `/healthz` are always
unauthenticated (they leak only the method list and liveness).

## Compatibility

- **`.vouch/` layout:** unchanged. No migration.
- **Bundle format / audit-log shape:** unchanged.
- **`kb.capabilities`:** `transports` array gains `"http"` when served over
  HTTP — additive; existing consumers that read the array keep working.
- **Method surface:** unchanged. CI's existing surface-parity test
  (`tests/test_capabilities.py`, which asserts every advertised method is
  reachable on every transport) is extended to cover HTTP.
- **Default behavior:** unchanged. `vouch serve` with no `--transport` is
  still stdio/MCP. HTTP is strictly opt-in.

## Security implications

This adds a network trust boundary, so it gets the most scrutiny.

- **Loopback by default.** Out of the box the server is unreachable off
  the host — same exposure as JSONL. Binding `0.0.0.0` (or any
  non-loopback address) is refused unless the operator passes both
  `--allow-public` and a token. "Accidentally exposed an unauthenticated
  KB to the LAN" should be impossible by default.
- **The review gate still applies.** HTTP clients file proposals like any
  other transport; `kb.approve` over HTTP is the same privileged operation
  it is everywhere and the `forbidden_self_approval` guard is unchanged. A
  network attacker who reaches `/rpc` with a valid token can approve
  proposals — so the token is a write-gate credential, documented as such.
- **No TLS in v1.** The stdlib server speaks plaintext HTTP. Public
  deployments MUST terminate TLS at a reverse proxy (nginx/Caddy). We
  document this rather than shipping a half-baked in-process TLS story.
- **CORS denied.** No `Access-Control-Allow-Origin` header — browsers
  can't cross-origin call the KB. Prevents a malicious page from driving a
  developer's loopback server.
- **Token handling.** Read from `--token` or `VOUCH_HTTP_TOKEN`; never
  logged, never echoed into the audit log. Constant-time comparison.
- **Out of scope for v1 (documented as such):** rate limiting, request
  size caps beyond a sane `Content-Length` ceiling, per-method authz
  (read-only vs write tokens), and audit of auth failures. Flagged as
  follow-ups so reviewers can decide what's blocking.

## Performance implications

Not on a hot path for the common (stdio) case — this is a new, opt-in
front-end. For HTTP itself: one `json.loads` + one dispatch per request,
identical cost to JSONL plus HTTP framing. `ThreadingHTTPServer` is fine
for the expected scale (a handful of clients against one KB); it is not a
high-throughput server and we don't claim it is. `state.db` is opened per
the existing store semantics; no new caching is introduced.

## Open questions (resolved)

- ~~**Bespoke REST vs MCP streamable-HTTP.**~~ Resolved: **both**. The
  vouch-native `/rpc` envelope ships for scripted clients that already
  speak it (no behavioural change vs PR #104), and `/mcp` is an
  MCP-over-Streamable-HTTP endpoint built by mounting the FastMCP ASGI
  app from the same kb.* surface. Five Claude surfaces that need the
  spec-compliant path (Claude.ai Custom Connectors, Claude mobile write,
  Managed Agents, Messages-API `mcp_servers`, Computer Use) drove this
  choice; #176 made it concrete.
- ~~**Default port.**~~ Resolved: `8731` (vouch's first deploy used it,
  no collisions reported, documented in `vouch serve --help`).
- ~~**Config vs flags.**~~ Resolved: **both**. CLI flags (`--token`,
  `--config`) are unchanged; `config.yaml` gains a `serve:` section:

  ```yaml
  serve:
    bearer_tokens:       # multi-token accept-list for fleet operators
      - alpha
      - beta
    # OR a single value, optionally via an env-var reference:
    bearer_token: env:VOUCH_TOKEN
  ```

  The flag and the config compose: any tokens from either source feed
  into the same accept-list. Multi-token rotation (mint a new token, add
  it to the list, retire the old one) is a strict ergonomic win for
  fleets where every agent has its own credential.
- **Per-method authz.** Still deferred — VEP-0005 (richer scopes on
  Claim/Source) is the right place to land it once it goes from draft
  to accepted, because the per-method authz model only matters once
  artifacts can be tagged with visibility scopes.

## Alternatives considered

- **Wrap JSONL in your own listener (status quo).** VEP-0002 explicitly
  says the JSONL transport has no auth and "if someone wraps it in a
  network listener, they need to add their own authentication." That works
  but pushes the bind/auth/exposure story onto every user and gives no
  documented, safe-by-default option. Issue #94 asks for first-class
  support precisely to avoid that.
- **A web framework (FastAPI/Flask + uvicorn).** Nicer ergonomics, but
  drags real dependencies into a project that has kept its runtime
  surface deliberately small. The stdlib server is enough for the scale.
- **MCP streamable-HTTP only.** Tighter fit for LLM hosts, but heavier to
  implement and wrong for the `curl`/CI/script consumers that motivated
  JSONL in the first place. Best handled as a follow-up VEP if demand
  appears — see open questions.
- **TLS in-process.** Rejected for v1: certificate handling in the CLI is
  a footgun; reverse-proxy TLS termination is the boring, correct answer.

## References

- Issue [#94](https://github.com/vouchdev/vouch/issues/94)
- [VEP-0002: JSONL transport](VEP-0002-jsonl-transport.md)
- [spec/transports.md](../spec/transports.md)
- [ROADMAP.md](../ROADMAP.md) — 0.1 line item, marked [VEP]
