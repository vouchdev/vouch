# Semantic Search Design — vouch

**Date:** 2026-05-20
**Status:** Approved (design)
**Branch:** `feat/semantic-search`
**Tracks issue:** (to be filed)

## 1. Goal

Add embedding-based semantic retrieval as the **primary** search backend in vouch's MCP, JSONL, and CLI search surfaces, with FTS5 as the deterministic fallback. Cover every artifact type (claims, sources, pages, entities, relations, evidence) with reranking, query expansion, duplicate detection, eval harness, and pluggable model adapters.

The existing search layer is FTS5 + substring (`src/vouch/index_db.py`, `src/vouch/storage.py:search_substring`). The current code even anticipates this addition — `index_db.py:8-9`:

> *"Vector search can be layered later as a second `backend` in the ContextItem response."*

This spec realizes that.

## 2. Integration shape

Decisions taken during brainstorming (all confirmed with the user):

| Axis | Choice |
|---|---|
| Integration mode | **Embedding as primary, FTS5 as fallback** (was: opt-in / hybrid-default / primary) |
| Compute timing | **Synchronous at write** (mirrors FTS5 today) |
| Default model | **`sentence-transformers/all-mpnet-base-v2`** — 768-dim, ~420MB, best quality at its tier |
| Vector store | **`sqlite-vec`** ANN, with **NumPy brute-force** fallback if the extension is unavailable |
| Default behavior | Behavior change: `kb.search` returns embedding hits first, FTS5 only if embedding returns none. Documented in changelog. |
| Scope | Maximally functional — all artifact types, reranking, HyDE, dedup, eval harness, multiple model adapters |

## 3. New package layout

```
src/vouch/embeddings/
  __init__.py                # public API: encode, search, register
  base.py                    # Embedder ABC + adapter registry
  st_mpnet.py                # default impl (sentence-transformers all-mpnet-base-v2)
  st_minilm.py               # alternative impl
  fastembed_bge.py           # alternative impl (no-torch path via fastembed)
  cache.py                   # query embedding LRU + content-hash skip cache
  rerank.py                  # cross-encoder reranker (ms-marco-MiniLM-L6-v2)
  hyde.py                    # Hypothetical Document Embedding query expansion
  dedup.py                   # cosine-threshold duplicate detection at ingest
  fusion.py                  # RRF, weighted-sum, normalized-cosine fusion strategies
  eval.py                    # recall@k / MRR / nDCG harness
  migration.py               # model-identity check + backfill orchestration
```

## 4. Storage layout (extends `.vouch/state.db`)

```sql
-- per-artifact ANN tables (sqlite-vec vec0 virtual tables)
CREATE VIRTUAL TABLE claim_vecs    USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE page_vecs     USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE source_vecs   USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE entity_vecs   USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE relation_vecs USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE evidence_vecs USING vec0(embedding float[768]);

-- mapping vec0 rowid <-> artifact id (+ content hash for skip-if-unchanged)
CREATE TABLE embedding_index (
  kind            TEXT NOT NULL,
  id              TEXT NOT NULL,
  rowid           INTEGER NOT NULL,
  content_hash    TEXT NOT NULL,
  model           TEXT NOT NULL,
  model_version   TEXT NOT NULL,
  dim             INTEGER NOT NULL,
  created_at      TEXT NOT NULL,
  PRIMARY KEY (kind, id)
);

-- model identity for mismatch detection
-- stored as rows in existing index_meta table:
--   ('embedding_model', 'sentence-transformers/all-mpnet-base-v2')
--   ('embedding_dim', '768')
--   ('embedding_lib', 'sentence-transformers')
--   ('embedding_lib_version', '<resolved>')

-- query embedding cache (content-addressed)
CREATE TABLE query_embedding_cache (
  query_hash      TEXT PRIMARY KEY,
  vec             BLOB NOT NULL,
  hit_count       INTEGER NOT NULL DEFAULT 1,
  last_used_at    TEXT NOT NULL
);

-- duplicate detection ledger (audit trail for ingest-time near-dupes)
CREATE TABLE embedding_dupes (
  kind            TEXT NOT NULL,
  id              TEXT NOT NULL,
  near_id         TEXT NOT NULL,
  cosine          REAL NOT NULL,
  detected_at     TEXT NOT NULL
);
```

`state.db` is derived. Losing it is non-fatal — `vouch reindex --embeddings --backfill` regenerates from disk. Same invariant as FTS5 today.

## 5. Touched modules and estimated LOC

| Module | Change | Est. LOC |
|---|---|---|
| `embeddings/base.py` | `Embedder` ABC, `register`, content hashing, batched encode | 120 |
| `embeddings/st_mpnet.py` | Default adapter | 80 |
| `embeddings/st_minilm.py` | Alternative adapter | 60 |
| `embeddings/fastembed_bge.py` | Alternative adapter (no-torch) | 80 |
| `embeddings/cache.py` | LRU query cache + persistent backing | 100 |
| `embeddings/rerank.py` | Cross-encoder reranker | 110 |
| `embeddings/hyde.py` | Template HyDE + optional LLM hook | 80 |
| `embeddings/dedup.py` | Ingest-time duplicate detection | 90 |
| `embeddings/fusion.py` | RRF, weighted, normalized-cosine fusion | 100 |
| `embeddings/eval.py` | recall@k / MRR / nDCG eval runner | 150 |
| `embeddings/migration.py` | Model-identity check + backfill orchestration | 110 |
| `index_db.py` | Vector tables, search fns, hybrid path, schema migration | 250 |
| `storage.py` | Hook all 6 `put_*` + `update_*` paths | 80 |
| `server.py` | Extended `kb_search` + 5 new MCP tools | 180 |
| `jsonl_server.py` | Parity for all new MCP tools | 160 |
| `cli.py` | Flags on existing commands + new commands | 250 |
| `context.py` | Semantic-default + `--explain` breakdown | 80 |
| `lifecycle.py` | Re-embed on `update_claim` / `update_page` | 60 |
| `pyproject.toml` | Three optional-deps extras | 15 |
| **Tests** | 9 files, ~900 LOC | 900 |
| **Total** | | **~3055 lines** |

## 6. Default behavior

| Call | Behavior |
|---|---|
| `kb.search(query)` | Embedding primary; FTS5 only if embedding returns zero hits. |
| `kb.search(query, backend="hybrid")` | RRF fusion of embedding + FTS5 result lists. |
| `kb.search(query, backend="hybrid", rerank=True)` | Hybrid + cross-encoder rerank of top-50. |
| `kb.search(query, hyde=True)` | Query expanded via HyDE template before encoding. |
| `kb.search(query, backend="fts5")` | Force lexical-only (precision mode). |
| `build_context_pack(task)` | Semantic-default; `--explain` returns per-result score breakdown. |

Every flag exposed identically across CLI / MCP / JSONL.

## 7. Write path

For every `put_*` and `update_*` in `KBStore`:

1. Compute `content_hash = sha256(text)`. If `embedding_index` has the same `(kind, id, content_hash)`, **skip encode** (idempotent re-ingest is free).
2. Otherwise: `Embedder.encode(text)` synchronously; persist to `<kind>_vecs` and `embedding_index`.
3. Run `dedup.check()` — cosine vs top-1 nearest neighbor. If ≥ `dedup_threshold` (default 0.95), log to `embedding_dupes` and emit `embedding.duplicate_detected` audit event. Ingest still proceeds.
4. Invalidate any `query_embedding_cache` entries known to reference this artifact (cache invalidation: drop entries by `last_used_at` age cutoff; full LRU eviction on cap).

## 8. Migration / backfill

On `KBStore.__init__`:

- Read `index_meta.embedding_model`. If absent (legacy KB) or mismatched with the current adapter:
  - Emit `embedding.model_mismatch` audit event.
  - `kb.search` still works via FTS5 fallback path; embedding results carry `embedding_stale: true` until reindex.
  - Maintainer-visible warning surfaces in `vouch doctor` and `vouch embeddings stats`.
- `vouch reindex --embeddings --backfill` does a single-pass re-encode of all artifacts under the current adapter, updates `index_meta`, drops `embedding_stale` tagging.

## 9. CLI surface

```bash
# search
vouch search "query"
vouch search "query" --semantic --top-k 20 --min-score 0.4
vouch search "query" --hybrid --rerank --hyde --explain
vouch search "query" --backend fts5            # force lexical

# reindex
vouch reindex --embeddings [--model NAME] [--backfill] [--force]

# eval
vouch eval embedding --queries eval/queries.jsonl --metric recall@10,mrr,ndcg

# dedup
vouch dedup --threshold 0.95 --dry-run

# stats
vouch embeddings stats        # model identity, vector counts, cache hit rate
```

## 10. MCP / JSONL parity

Every new flag/command exposed identically as a tool:

- `kb.search` gains: `backend`, `top_k`, `min_score`, `rerank`, `hyde`, `explain`
- New tools: `kb.reindex_embeddings`, `kb.eval_embeddings`, `kb.dedup_scan`, `kb.embeddings_stats`

JSONL handlers mirror the MCP tools 1:1 (same method names, same param names).

## 11. Dependencies

```toml
# pyproject.toml
[project.optional-dependencies]
embeddings       = ["sentence-transformers>=2.7", "numpy>=1.26", "sqlite-vec>=0.1"]
embeddings-fast  = ["fastembed>=0.3", "onnxruntime>=1.18", "sqlite-vec>=0.1"]
rerank           = ["sentence-transformers>=2.7"]   # shared base with embeddings
```

Base install stays lean. CI matrix exercises all three install modes (none / `[embeddings]` / `[embeddings-fast]`).

## 12. Default values (tunable via config)

| Knob | Default | Source |
|---|---|---|
| Model cache location | `~/.cache/vouch/models/` | env `VOUCH_MODEL_CACHE` override |
| Embedding dimension | 768 (matches mpnet) | derived from model |
| Dedup threshold | 0.95 cosine | `config.yaml` |
| Rerank top-K | 50 | CLI flag |
| HyDE template | template-only (no LLM) | CLI flag enables LLM hook |
| Query cache size | 1024 LRU entries | `config.yaml` |
| Backend ordering | `["embedding", "fts5"]` | `config.yaml` |

## 13. Test plan (~900 LOC across 9 files)

| File | Covers |
|---|---|
| `tests/test_embeddings_core.py` | Embedder ABC, registry, content-hash skip, batched encode, lazy load |
| `tests/test_embeddings_storage.py` | vec0 + sqlite-vec round trip + NumPy fallback parity |
| `tests/test_embeddings_search.py` | Semantic primary, FTS5 fallback, lexical-disjoint regression |
| `tests/test_embeddings_fusion.py` | RRF, weighted, normalized fusion strategies correctness |
| `tests/test_embeddings_rerank.py` | Cross-encoder rerank changes top-K order on a known pair |
| `tests/test_embeddings_hyde.py` | HyDE expansion improves recall on terse queries |
| `tests/test_embeddings_dedup.py` | Threshold ledger + audit event |
| `tests/test_embeddings_migration.py` | Model-version mismatch + backfill flow |
| `tests/test_embeddings_eval.py` | recall@k / MRR / nDCG correctness on synthetic ground truth |
| `tests/test_embeddings_cli.py` | CLI flag routing |

## 14. Acceptance criteria

- [ ] `vouch search --semantic "how do we authenticate users"` returns a claim that says *"login flow uses session cookies signed by the API"* in a KB with no lexical overlap.
- [ ] `pip install vouch` (no extras) still works and uses FTS5/substring without errors.
- [ ] `pip install vouch[embeddings]` enables the full embedding stack with no other code changes required.
- [ ] `vouch reindex --embeddings --backfill` is idempotent; running twice yields the same `embedding_index` row count.
- [ ] All 9 test files pass; `ruff` and `mypy` clean.
- [ ] CI matrix runs `(base, [embeddings], [embeddings-fast])` and all three modes pass.
- [ ] Model-identity mismatch (delete `state.db`, change `embedding_model` in `index_meta`) produces a clear warning, NOT a crash.

## 15. Out of scope (genuinely orthogonal — separate spec)

- Multi-language / cross-lingual models (deferred to a follow-up; current scope is English).
- Distributed embedding compute (everything stays in-process).
- Online learning / fine-tuning hooks (consumers can bring their own adapter via the registry).
- Replacement of FTS5 (FTS5 stays as fallback / precision-mode forever).

## 16. Rollout order (suggested for implementation plan)

The `writing-plans` step will turn this into concrete tasks. Suggested phases:

1. **Foundation** — `embeddings/base.py`, default adapter, `pyproject.toml` extras
2. **Storage** — `index_db.py` vec tables, NumPy fallback, schema migration
3. **Write path** — `storage.py` hook on `put_claim` (first artifact type); extend to remaining 5
4. **Read path** — `index_db.search_embedding`, integrate into `kb.search` (MCP + JSONL + CLI)
5. **Fusion + hybrid** — `embeddings/fusion.py`, hybrid backend
6. **Rerank, HyDE, dedup, eval, migration** — independent capability slices
7. **Context pack + explain** — `context.py` updates
8. **CLI + JSONL parity sweep** — `vouch search/reindex/eval/dedup/embeddings`

Each phase ends with passing tests for its slice — no big-bang merge.
