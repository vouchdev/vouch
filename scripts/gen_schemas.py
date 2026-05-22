"""Regenerate JSON Schema files in `schemas/` from the pydantic models.

The pydantic models in `src/vouch/models.py` are the source of truth.
The JSON Schemas under `schemas/` are the **interop contract**: any
AKBP-compatible tool can validate vouch artifacts without reading
vouch's Python source. This script keeps the two in sync.

Run from the repo root:

    python scripts/gen_schemas.py

`bundle.manifest.schema.json` and `jsonl-envelope.schema.json` have no
pydantic counterpart and are hand-maintained — this script leaves them
alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from vouch.models import (
    AuditEvent,
    Capabilities,
    Claim,
    ContextItem,
    ContextPack,
    ContextQuality,
    Entity,
    Evidence,
    Page,
    Proposal,
    Relation,
    Session,
    Source,
)

# slug → pydantic model. Slug is the on-disk filename stem and the
# stable identifier in the schema $id.
MODELS = {
    "source": Source,
    "evidence": Evidence,
    "claim": Claim,
    "entity": Entity,
    "relation": Relation,
    "page": Page,
    "session": Session,
    "audit-event": AuditEvent,
    "proposal": Proposal,
    "context-item": ContextItem,
    "context-quality": ContextQuality,
    "context-pack": ContextPack,
    "capabilities": Capabilities,
}

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def main() -> None:
    SCHEMAS_DIR.mkdir(exist_ok=True)
    for slug, model in MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://vouch.dev/schemas/{slug}.schema.json"
        out = SCHEMAS_DIR / f"{slug}.schema.json"
        out.write_text(json.dumps(schema, indent=2, sort_keys=True))
    print(f"regenerated {len(MODELS)} schemas in {SCHEMAS_DIR}")


if __name__ == "__main__":
    main()
