# Four search backends and a score floor

`vouch search` picks a backend — `auto`, `fts5`, `substring`, `embedding`, or
`hybrid` — and the `--json` output reports which one actually ran. This example
seeds a few approved claims and runs the same `jwt` query through each backend
so you can watch the `backend` field change and see how `--min-score` trims weak
hits. The embedding and hybrid legs degrade to a skip notice when the optional
`vouch-kb[embeddings]` extra is absent.

## Run it:

```bash
./examples/search-backends/run.sh
```

It builds a throwaway KB in a temp dir, so it leaves nothing behind. Override the
binary with `VOUCH=/path/to/vouch ./examples/search-backends/run.sh`.

## Steps

1. `vouch init` a fresh KB.
2. Record a source, then propose four `fact` claims that cite it — all as
   `alice-example`. Each claim must point at a recorded source id; vouch rejects
   an unknown evidence id, and the review gate forbids self-approval.
3. Approve the batch as a separate reviewer (`reviewer-example`) and rebuild the
   index.
4. Run `vouch search jwt --backend fts5 --json` and `--backend substring --json`.
   Both return the two jwt claims; the `backend` field reports `fts5` vs
   `substring`, and the scores differ (fts5 is a ranked relevance score,
   substring is a flat `1.0` match).
5. Guard the semantic legs: detect whether `sentence_transformers` is importable.
   When present, run `--backend embedding` and `--backend hybrid --min-score 0.2`;
   when absent, print `embedding backend skipped: install vouch-kb[embeddings]`.
   On a base install the embedding backend simply returns zero hits, so the guard
   keeps the example honest about what ran.
6. `vouch stats --json` to read citation coverage — all four claims cite the
   source, so coverage is 100%.

## Real output (base install, no embeddings extra)

```text
== query 'jwt' through --backend fts5 ==
  backend=fts5  hits=2
    claim/jwt-access-tokens-expire-after-15-minutes-at-acme-example  score=0.35427051614021915
    claim/the-jwt-signing-key-is-rotated-quarterly-via-the-secrets-man  score=0.3413723177370558

== query 'jwt' through --backend substring ==
  backend=substring  hits=2
    claim/jwt-access-tokens-expire-after-15-minutes-at-acme-example  score=1.0
    claim/the-jwt-signing-key-is-rotated-quarterly-via-the-secrets-man  score=1.0

== embedding + hybrid backends skipped ==
  embedding backend skipped: install vouch-kb[embeddings] for semantic + hybrid search

== citation coverage from vouch stats ==
  claims with valid citations: 5/5 (100%)

search-backends example passed
```

(The init step also seeds one starter claim, so `stats` reports 5 claims total.)
With `vouch-kb[embeddings]` installed, the embedding and hybrid sections run too,
reporting `backend=embedding` and `backend=hybrid`, and `--min-score 0.2` drops
any fused hit scoring below the floor.

## Methods demonstrated:

- `kb.search` — `--backend fts5 | substring | embedding | hybrid`, `--min-score`,
  `--json` (the `backend` field reports what actually ran)
- `kb.stats` — citation coverage over the approved corpus
