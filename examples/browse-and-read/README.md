# Browse and read every artifact kind

The read-side mirror of the propose family: `list_*` enumerates each artifact
kind, `read_*` fetches one by id, and `cite` resolves the evidence backing a
claim.

## Run it

```bash
VOUCH=/path/to/vouch ./examples/browse-and-read/run.sh
```

`VOUCH` defaults to `vouch` on your `PATH`. The script builds a throwaway KB in
a `mktemp` directory, populates it, runs every reader against it, and deletes
the KB on exit. Nothing touches your real `.vouch/`.

## What it builds

A vouch KB holds five kinds of artifact. To exercise all four `read_*` fetchers
and all five `list_*` enumerators, the example first seeds one of each — every
write going through the review gate:

1. `kb.register_source` — a source record (the evidence).
2. `kb.propose_claim` + `kb.approve` — a claim citing that source.
3. `kb.propose_entity` + `kb.approve` — twice (`alice-example`, `acme-example`),
   so a relation has two endpoints.
4. `kb.propose_relation` + `kb.approve` — a relation between the two entities.
5. `kb.propose_page` + `kb.approve` — a page that links the claim.

Proposals are written as `example-agent`. vouch's review gate forbids
self-approval, so the approvals run under a second identity, `reviewer-example`
(set via `VOUCH_AGENT` on the approve calls). That is the load-bearing
invariant on display: a different actor reviews the write.

## Then it reads

For each kind the script enumerates with `list_*`, prints the full list, then
fetches a single artifact by id with the matching `read_*`. Finally it calls
`kb.cite` on the claim to resolve the source record behind it.

```text
=== kb.list_claims -> kb.read_claim ===
{"id": "lc", "ok": true, "result": [{"id": "acme-ships-deploys-behind-a-feature-flag-by-default", "text": "Acme ships deploys behind a feature flag by default.", "type": "observation", "status": "working", "confidence": 0.9, "evidence": ["f3109085...c118902"], ... "approved_by": "reviewer-example"}, {"id": "vouch-starter-reviewed-knowledge", ...}]}
{"id": "rc", "ok": true, "result": {"id": "acme-ships-deploys-behind-a-feature-flag-by-default", ...}}

=== kb.list_relations -> kb.read_relation ===
{"id": "lr", "ok": true, "result": [{"id": "alice-example-relates-to-acme-example", "source": "alice-example", "relation": "relates_to", "target": "acme-example", "confidence": 0.9, ...}]}
{"id": "rr", "ok": true, "result": {"id": "alice-example-relates-to-acme-example", ...}}

=== kb.cite (evidence backing the claim) ===
{"id": "ct", "ok": true, "result": [{"kind": "source", "source_id": "f3109085...c118902", "title": "acme-deploy-policy", "locator": "https://example.com/acme/deploy-policy", "hash": "f3109085...c118902"}]}

=== done. temp KB cleaned up. ===
```

(The starter claim/page/source seeded by `vouch init` also show up in the
`list_*` output — proof the enumerators return everything durable, not just the
artifacts this script wrote.)

## Notes on the readers

- `list_*` takes no required params; `list_claims` accepts an optional
  `status`, `list_entities` an `entity_type`, `list_relations` a `node_id`
  filter.
- `read_claim`/`read_page`/`read_entity`/`read_relation` each take the one id
  param (`claim_id`, `page_id`, `entity_id`, `relation_id`) and return the full
  artifact.
- `cite` takes `claim_id` and returns the resolved source rows backing the
  claim's `evidence` list — the bridge from a claim back to its provenance.

## Methods demonstrated

- `kb.list_pages`
- `kb.list_claims`
- `kb.list_entities`
- `kb.list_relations`
- `kb.list_sources`
- `kb.read_page`
- `kb.read_claim`
- `kb.read_entity`
- `kb.read_relation`
- `kb.cite`

(plus `kb.register_source`, `kb.propose_*`, and `kb.approve` to build the KB.)
