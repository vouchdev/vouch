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
