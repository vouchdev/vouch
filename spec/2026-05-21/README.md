# spec — snapshot 2026-05-21

Frozen copy of the vouch specification at version `2026-05-21`. Files
here MUST NOT be edited; if the spec needs to change, edit the
top-level [../../SPEC.md](../../SPEC.md) and the chapter files under
[../](../), then cut a new dated snapshot.

| File | Topic |
|---|---|
| [SPEC.md](SPEC.md) | Top-level specification |
| [methods.md](methods.md) | Per-method parameter + result shapes |
| [review-gate.md](review-gate.md) | Proposal / decision state machine |
| [transports.md](transports.md) | MCP and JSONL framing + errors |
| [audit-vocabulary.md](audit-vocabulary.md) | Canonical event names |
| [retrieval.md](retrieval.md) | FTS5 schema and scoring |

JSON Schemas corresponding to this snapshot live in
[../../schemas/](../../schemas/) and can be regenerated from the pydantic
models with `python scripts/gen_schemas.py`.
