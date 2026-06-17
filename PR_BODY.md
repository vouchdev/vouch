# feat(synthesize): `kb.synthesize` answer-mode retrieval over the review-gated KB

## What changed

Adds `kb.synthesize` — an answer-mode counterpart to `kb.context`. Where
`kb.context` returns a *ranked list* of relevant items, `kb.synthesize`
answers a query in prose, but strictly from **approved (durable) claims**,
with an inline `[claim_id]` citation behind every sentence.

New surface, wired across all three transports that the capabilities test
keeps in sync:

- `src/vouch/synthesize.py` — `synthesize(store, *, query, depth=3,
  max_chars=4000, llm=False)`. Walks `build_context_pack(... limit=depth)`,
  keeps only `claim` items that resolve to a durable claim via
  `store.get_claim`, and composes a deterministic answer: one short,
  single-clause sentence per claim, each carrying at least one `[claim_id]`
  citation. No sentence is emitted that isn't traceable to a claim id.
  `max_chars` truncates by dropping trailing claims (never by cutting a
  citation). Returns
  `{"query", "answer", "claims", "gaps", "_meta": {"synthesis_confidence"}}`.
  `gaps` lists the query's salient terms for which no approved claim was
  found (and is the whole answer when nothing matched). `synthesis_confidence`
  is `high` when every cited claim is `stable`, `medium` when any is
  `working`/`actionable`, `low` when any is `contested`. `llm=True` raises
  (reserved for an opt-in generative backend; deterministic synthesis is the
  v1 default).
- `src/vouch/capabilities.py` — `kb.synthesize` appended to `METHODS`.
- `src/vouch/jsonl_server.py` — `_h_synthesize` handler + `HANDLERS` entry.
- `src/vouch/server.py` — `@mcp.tool() kb_synthesize(query, depth=3,
  max_chars=4000)`.
- `src/vouch/cli.py` — `vouch synthesize "<query>" [--depth N] [--max-chars N]`.
- `CHANGELOG.md` — `### Added` bullet under `## [Unreleased]`.

## Why / root cause

`kb.context` is a retrieval primitive: it ranks and budgets items but leaves
answer composition (and the discipline of *only* using approved knowledge) to
the caller. There was no first-class way to ask the KB a question and get a
prose answer whose every clause is provably backed by a reviewed claim, with
the uncovered parts of the question surfaced rather than silently dropped.
`kb.synthesize` fills that gap deterministically — citation-gated by
construction, so it cannot fabricate an unbacked sentence — and grades its own
confidence from the lifecycle status of the claims it actually cited.

## Test plan

`tests/test_synthesize.py` covers:

- 3 approved `auth` claims → non-empty answer citing all 3 ids by `[id]`,
  confidence `high`.
- A query the KB doesn't cover → `answer == ""`, `claims == []`, `gaps`
  populated with the query's salient terms.
- Fuzz/traceability: every sentence in a non-empty answer carries at least one
  `[id]` citation whose id is in `claims` and resolves via `store.get_claim`.
- `max_chars` drops trailing claims without cutting a citation
  (citation count == cited-claim count).
- Confidence reflects claim status (`working` → medium, `contested` → low).
- `llm=True` raises the reserved-backend `ValueError`.
- `kb.synthesize` is in `capabilities().methods` and in the JSONL `HANDLERS`,
  and is callable via `handle_request` end-to-end.

Verification gate (fresh venv, editable install of this worktree):

```
$ ./.venv/bin/ruff check src tests
All checks passed!

$ ./.venv/bin/mypy src
Success: no issues found in 30 source files

$ ./.venv/bin/python -m pytest -q
94 passed, 6 skipped in 0.81s
```

(The 6 skips are pre-existing numpy/embedding-optional tests, unrelated to this
change.)

Closes #222
