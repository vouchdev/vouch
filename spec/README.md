# spec/

Chapter-length specification documents. The top-level [SPEC.md](../SPEC.md)
is the entry point; this directory holds the longer reference material.

| File | Topic |
|---|---|
| [methods.md](methods.md) | Per-method parameter + result shapes for the `kb.*` surface |
| [review-gate.md](review-gate.md) | Full state machine for proposals, decisions, lifecycle |
| [transports.md](transports.md) | MCP-over-stdio and JSONL framing, error codes |
| [audit-vocabulary.md](audit-vocabulary.md) | Canonical event names emitted to `audit.log.jsonl` |
| [retrieval.md](retrieval.md) | FTS5 schema, scoring, future embedding hooks |

These are *informative* where they elaborate on SPEC.md and *normative*
where they define vocabulary (event names, error codes, JSON shapes).

## Versioning

The files at the top of this directory and `../SPEC.md` are the **latest**
working draft. Dated subdirectories (`2026-05-21/`, …) are immutable
snapshots — useful when implementations need to pin to a specific
protocol version. Cut a new snapshot whenever you ship a breaking
change to method shapes, on-disk layout, or vocabulary.

| Version | Status | Notes |
|---|---|---|
| [2026-05-21](2026-05-21/) | current | initial dated snapshot |
