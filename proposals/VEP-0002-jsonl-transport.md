---
vep: 0002
title: JSONL transport
author: plind-junior
status: final
created: 2026-05-03
landed-in: 0.0.1
---

# VEP-0002: JSONL transport

## Summary

Add a newline-delimited JSON transport (`vouch serve --transport jsonl`)
alongside the default MCP-over-stdio transport. Same `kb.*` method
surface; trivially scriptable.

## Motivation

MCP is the right transport for LLM hosts (Claude Code, Cursor, Codex).
It is the wrong transport for:

- CI jobs that want to check pending proposals or run `kb.lint`.
- Shell scripts that want to register a source from `curl` output.
- People learning vouch from a terminal who want to type `echo
  '{"id":"r1","method":"kb.status"}' | vouch serve --transport jsonl`
  and see what happens.

A second transport with zero protocol dependencies (no MCP, no
JSON-RPC, no HTTP, no schema negotiation) makes vouch testable and
embeddable without dragging in a host.

## Proposal

Add `--transport {stdio,jsonl}` to `vouch serve`. Default remains
`stdio` (MCP). The JSONL transport reads one envelope per line on
stdin and writes one envelope per line on stdout.

Request shape:

```json
{"id": "r1", "method": "kb.search", "params": {"query": "jwt"}}
```

Response shape (success):

```json
{"id": "r1", "ok": true, "result": [...]}
```

Response shape (error):

```json
{"id": "r1", "ok": false, "error": {"code": "missing_param", "message": "..."}}
```

Error codes: `method_not_found`, `missing_param`, `invalid_request`,
`internal_error`.

## Design

The MCP server and JSONL server share a single internal dispatch
table. Both transports go through `_handle_request(method, params,
actor) -> Result | Error`. Adding a method to the surface registers it
once and both transports pick it up.

`tests/test_capabilities.py` asserts the surfaces match exactly: every
method advertised by `kb.capabilities` is reachable on both
transports. This is enforced in CI.

## Compatibility

Pure addition. No existing files or methods change.

## Security implications

The JSONL transport has no built-in authentication. It is intended for
stdin/stdout use — caller and callee are in the same process tree.

If someone wraps it in a network listener, they need to add their own
authentication. The spec is explicit about this: see
[spec/transports.md](../spec/transports.md).

## Performance implications

JSONL adds no overhead beyond JSON parsing per line. For local use,
it's strictly cheaper than MCP because there's no JSON-RPC framing.

## Open questions

Resolved:
- ~~Should we use JSON-RPC 2.0 framing?~~ No. The `{ok, result/error}`
  shape is simpler and avoids the JSON-RPC `error.code` integer
  vocabulary (which is meaningless for our domain).
- ~~Should `id` be optional?~~ No. Required, even for fire-and-forget,
  so error replies are always correlatable.

## Alternatives considered

**Just MCP.** Forces every consumer to speak MCP. Bad for scripts and
CI; also bad for people who want to read the protocol off a man page.

**HTTP only.** Adds bind/auth concerns. Real plan for 0.1 (see ROADMAP),
but not in place of stdio JSONL — in addition to it.

**JSON-RPC 2.0.** Considered; rejected for the reason above. We can
add a JSON-RPC adapter later if a consumer needs it.

## References

- [spec/transports.md](../spec/transports.md)
- [schemas/jsonl-envelope.schema.json](../schemas/jsonl-envelope.schema.json)
