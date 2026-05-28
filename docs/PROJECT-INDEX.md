# vouch — Project Index

> Git-native, review-gated knowledge base for LLM agents.
> MCP server + JSONL tool server + CLI.

**Package:** `vouch-kb` (PyPI) | **CLI command:** `vouch` | **Version:** 0.1.0 (alpha)
**Language:** Python 3.11+ | **Build:** Hatchling | **License:** MIT

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Module Map](#module-map)
3. [Object Model](#object-model)
4. [Data Flow](#data-flow)
5. [Storage Layout](#storage-layout)
6. [Transport Surfaces](#transport-surfaces)
7. [CLI Reference](#cli-reference)
8. [MCP / JSONL Tool Surface](#mcp--jsonl-tool-surface)
9. [Embeddings Stack](#embeddings-stack)
10. [Test Structure](#test-structure)
11. [CI/CD](#cicd)
12. [Supporting Directories](#supporting-directories)
13. [Dependencies](#dependencies)
14. [Codebase Statistics](#codebase-statistics)

---

## Architecture Overview

```
                        +------------------+
                        |   LLM Agents     |
                        | (Claude, Cursor,  |
                        |  Codex, etc.)    |
                        +--------+---------+
                                 |
                  +--------------+--------------+
                  |                             |
           +------v------+            +--------v--------+
           | MCP Server  |            | JSONL Server    |
           | (server.py) |            | (jsonl_server.py)|
           | FastMCP/stdio|           | stdin/stdout    |
           +------+------+            +--------+--------+
                  |                             |
                  +--------------+--------------+
                                 |
                        +--------v--------+
                        |  Business Logic |
                        |  proposals.py   |
                        |  lifecycle.py   |
                        |  sessions.py    |
                        |  context.py     |
                        +--------+--------+
                                 |
                  +--------------+--------------+
                  |                             |
           +------v------+            +--------v--------+
           | Storage     |            | Index (SQLite)  |
           | (storage.py)|            | (index_db.py)   |
           | YAML/MD on  |            | FTS5 + embedding|
           | disk        |            | state.db        |
           +------+------+            +--------+--------+
                  |                             |
           +------v------+            +--------v--------+
           | Audit Log   |            | Embeddings      |
           | (audit.py)  |            | (embeddings/)   |
           | JSONL append|            | pluggable models|
           +-------------+            +-----------------+

                  +------------------+
                  |   CLI (cli.py)   |
                  |   Click commands |
                  |   Human review   |
                  +------------------+
```

**Core principle:** Agents *propose*, humans *approve*. Files on disk are the source of truth. SQLite is a derived cache. Everything is audited.

---

## Module Map

### `src/vouch/` — Core Package (27 files, ~4,500 LOC)

| Module | Purpose | Key Classes/Functions |
|---|---|---|
| `__init__.py` | Package root, version | `__version__` |
| `models.py` | Pydantic domain models (AKBP v0.1 compatible) | `Source`, `Evidence`, `Claim`, `Entity`, `Relation`, `Page`, `Session`, `AuditEvent`, `Proposal`, `ContextPack`, `Capabilities` |
| `storage.py` | File-backed CRUD (YAML/MD on disk) | `KBStore`, `discover_root()`, `ArtifactNotFoundError`, `KBNotFoundError` |
| `server.py` | MCP server (FastMCP over stdio) | `mcp`, `kb_*` tool functions, `run_stdio()` |
| `jsonl_server.py` | JSONL tool server (stdin/stdout) | `HANDLERS` dict, `handle_request()`, `run_jsonl()` |
| `cli.py` | Click CLI for humans | `cli` group, 30+ commands |
| `proposals.py` | Review gate: propose -> approve/reject | `propose_claim()`, `propose_page()`, `propose_entity()`, `propose_relation()`, `approve()`, `reject()` |
| `lifecycle.py` | Claim lifecycle mutations (direct, audited) | `supersede()`, `contradict()`, `archive()`, `confirm()`, `cite()` |
| `sessions.py` | Agent session lifecycle | `session_start()`, `session_end()`, `crystallize()` |
| `context.py` | ContextPack assembly for agent prompts | `build_context_pack()` |
| `index_db.py` | SQLite FTS5 index + embedding storage | `open_db()`, `search()`, `search_semantic()`, `search_embedding()` |
| `audit.py` | Append-only JSONL audit log | `log_event()`, `read_events()` |
| `health.py` | Status, lint, doctor, index rebuild | `status()`, `lint()`, `doctor()`, `rebuild_index()` |
| `verify.py` | Source integrity verification (hash drift) | `verify_source()`, `verify_all()` |
| `bundle.py` | Portable tar.gz export/import with manifest | `export()`, `import_check()`, `import_apply()`, `export_check()` |
| `capabilities.py` | AKBP capabilities descriptor | `capabilities()`, `METHODS` list |
| `onboarding.py` | Starter KB content for `vouch init` | `seed_starter_kb()` |

### `src/vouch/embeddings/` — Semantic Retrieval (12 files)

| Module | Purpose |
|---|---|
| `__init__.py` | Registry, auto-discovers adapters |
| `base.py` | `Embedder` protocol, `register()` / `get_embedder()` registry |
| `st_mpnet.py` | sentence-transformers `all-mpnet-base-v2` adapter (default) |
| `st_minilm.py` | sentence-transformers `all-MiniLM-L6-v2` adapter |
| `fastembed_bge.py` | FastEmbed BGE adapter (ONNX-backed) |
| `cache.py` | Query embedding cache (SQLite-backed) |
| `fusion.py` | Reciprocal Rank Fusion (RRF) for hybrid search |
| `hyde.py` | Hypothetical Document Embedding query expansion |
| `rerank.py` | Cross-encoder reranking |
| `dedup.py` | Near-duplicate detection via cosine similarity |
| `scorer.py` | Retrieval evaluation (recall@k, MRR, NDCG) |
| `migration.py` | Embedding model migration / backfill |

---

## Object Model

```
Source (immutable, content-addressed by sha256)
  |
  +-- Evidence (span pointer: line range, timestamp, quote)
  |
  +-- Claim (atomic assertion, typed, status-tracked, confidence-scored)
  |     |-- cites: [Source/Evidence ids]  (mandatory, >=1)
  |     |-- status: working -> actionable -> stable -> contested/superseded/archived
  |     |-- approved_by: review gate audit trail
  |     +-- type: fact | decision | preference | workflow | observation | question | warning
  |
  +-- Entity (typed node: person, project, repo, concept, ...)
  |
  +-- Relation (typed edge: uses, depends_on, supersedes, contradicts, ...)
  |
  +-- Page (markdown document with YAML frontmatter, links claims/entities)
  |
  +-- Session (agent work block, bundles proposals)
  |
  +-- Proposal (pending write, gated by review)
  |     |-- kind: claim | page | entity | relation
  |     +-- status: pending -> approved | rejected
  |
  +-- AuditEvent (append-only log entry for every mutation)
  |
  +-- ContextPack (retrieval result bundle with quality gate)
```

### Enums

| Enum | Values |
|---|---|
| `SourceType` | file, url, transcript, message, commit, issue, screenshot, pdf, audio, video, folder |
| `ClaimType` | fact, decision, preference, workflow, observation, question, warning |
| `ClaimStatus` | working, actionable, stable, contested, superseded, archived, redacted |
| `EntityType` | person, project, repo, company, concept, decision, workflow, file, api, incident, source, agent, tool, team, system |
| `RelationType` | uses, depends_on, contradicts, supersedes, supports, caused_by, owned_by, derived_from, similar_to, blocks, implements, references |
| `PageType` | entity, concept, decision, workflow, session, index, log, report, source-summary |
| `Scope` | private, project, team, public |

---

## Data Flow

### Write Path (Review-Gated)

```
Agent calls kb_propose_claim(text, evidence, ...)
  |
  v
proposals.py: propose_claim()
  |-- validates evidence ids exist (Source or Evidence)
  |-- generates slug from text
  |-- creates Proposal(kind=CLAIM, status=PENDING)
  |-- storage.put_proposal() -> .vouch/proposed/<id>.yaml (gitignored)
  |-- audit.log_event("proposal.claim.create")
  v
Human runs `vouch approve <id>`
  |
  v
proposals.py: approve()
  |-- checks proposal is PENDING
  |-- checks approved_by != proposed_by (unless trusted-agent config)
  |-- checks no existing artifact with that id
  |-- creates Claim from payload
  |-- storage.put_claim() -> .vouch/claims/<id>.yaml (committed)
  |-- index_db.index_claim() -> state.db FTS5
  |-- storage._embed_and_store() -> state.db embedding_index
  |-- moves proposal: proposed/ -> decided/
  |-- audit.log_event("proposal.claim.approve")
```

### Read Path (Unrestricted)

```
Agent calls kb_search(query, backend="auto")
  |
  v
1. Try semantic search (index_db.search_semantic)
   |-- embeddings.get_embedder().encode(query)
   |-- index_db.search_embedding() via sqlite-vec or NumPy cosine
   |
2. Fallback: FTS5 search (index_db.search)
   |-- BM25 ranking across claims_fts, pages_fts, entities_fts
   |
3. Fallback: substring scan (storage.search_substring)
   |-- brute-force text match across all artifacts
```

### Session + Crystallize Flow

```
kb_session_start(task="...")
  |-> Session record in .vouch/sessions/

Agent proposes claims/pages/entities during session
  |-> each Proposal gets session_id

kb_session_end(session_id)
  |-> backfills proposal_ids

kb_crystallize(session_id)
  |-> approve() every PENDING proposal in session
  |-> write session-summary Page
  |-> audit everything
```

---

## Storage Layout

```
<project-root>/
  .vouch/
  |-- config.yaml                    # KB settings (version, review policy, retrieval config)
  |-- .gitignore                     # ignores proposed/, state.db
  |-- audit.log.jsonl                # append-only audit (committed)
  |-- state.db                       # SQLite: FTS5 + embeddings (derived, gitignored)
  |-- claims/<id>.yaml               # durable approved claims
  |-- pages/<id>.md                  # markdown pages with YAML frontmatter
  |-- sources/<sha256>/
  |   |-- meta.yaml                  # source metadata
  |   +-- content                    # raw source bytes
  |-- entities/<id>.yaml             # graph nodes
  |-- relations/<id>.yaml            # graph edges
  |-- evidence/<id>.yaml             # citation pointers into sources
  |-- sessions/<id>.yaml             # session records
  |-- proposed/<id>.yaml             # pending proposals (gitignored, local-only)
  +-- decided/<id>.yaml              # approved/rejected proposals (committed)
```

**Key invariant:** Files are the source of truth. `state.db` is a derived cache rebuilt by `vouch index`. Losing it is never fatal.

---

## Transport Surfaces

### 1. MCP Server (`server.py`)

- Transport: stdio (FastMCP)
- Entry: `vouch serve` or configured in `.mcp.json`
- Agent identification: `VOUCH_AGENT` env var
- 43 tools registered via `@mcp.tool()` decorators

### 2. JSONL Server (`jsonl_server.py`)

- Transport: newline-delimited JSON over stdin/stdout
- Entry: `vouch serve --transport jsonl`
- Same tool surface as MCP, different wire format
- Request: `{"id": "r1", "method": "kb.search", "params": {...}}`
- Response: `{"id": "r1", "ok": true, "result": {...}}`

### 3. CLI (`cli.py`)

- Framework: Click
- Entry: `vouch` command (registered via `[project.scripts]`)
- Actor: `VOUCH_AGENT` > `VOUCH_USER` > OS username
- Human-facing review interface

---

## CLI Reference

### Bootstrap
| Command | Description |
|---|---|
| `vouch init [--path P]` | Initialize `.vouch/` KB with starter content |
| `vouch discover [--path P]` | Find nearest `.vouch/` root |
| `vouch capabilities` | Emit JSON capabilities descriptor |

### Health & Status
| Command | Description |
|---|---|
| `vouch status [--json]` | Artifact counts + pending proposals |
| `vouch lint [--stale-days N]` | Broken citations, stale claims, dangling refs |
| `vouch doctor` | Full sweep: lint + source verify + index check |

### Review Gate
| Command | Description |
|---|---|
| `vouch pending` | List proposals awaiting review |
| `vouch show <id>` | Full proposal details |
| `vouch approve <id> [--reason]` | Approve -> durable artifact |
| `vouch reject <id> --reason "..."` | Reject with reason |

### Propose (CLI shortcuts)
| Command | Description |
|---|---|
| `vouch propose-claim --text ... --source ...` | Propose a claim |
| `vouch propose-page --title ... [--body -]` | Propose a page |
| `vouch propose-entity --name ... --type ...` | Propose an entity |
| `vouch propose-relation --from ... --rel ... --to ...` | Propose a relation |

### Sources
| Command | Description |
|---|---|
| `vouch source add PATH` | Register file as Source |
| `vouch source verify [--fail-on-issue]` | Re-hash and detect drift |

### Lifecycle
| Command | Description |
|---|---|
| `vouch supersede OLD NEW` | Mark old claim superseded by new |
| `vouch contradict A B` | Record contradiction (symmetric) |
| `vouch archive CLAIM_ID` | Archive a claim |
| `vouch confirm CLAIM_ID` | Re-confirm (bumps last_confirmed_at) |
| `vouch cite CLAIM_ID` | Resolve citations |

### Sessions
| Command | Description |
|---|---|
| `vouch session start [--task ...] [--note ...]` | Start agent session |
| `vouch session end SESSION_ID` | End session |
| `vouch crystallize SESSION_ID [--no-page]` | Approve all pending + summary page |

### Retrieval
| Command | Description |
|---|---|
| `vouch search QUERY [--backend ...] [--rerank] [--hyde]` | Search KB |
| `vouch context TASK [--max-chars N]` | Build ContextPack for agent prompt |
| `vouch index` | Rebuild state.db |
| `vouch reindex [--embeddings] [--backfill]` | Rebuild FTS5 + optional embedding backfill |
| `vouch dedup [--threshold 0.95]` | Near-duplicate scan |

### Embeddings
| Command | Description |
|---|---|
| `vouch embeddings stats` | Model identity, counts, cache stats |
| `vouch eval embedding --queries FILE` | Retrieval quality metrics |

### Audit
| Command | Description |
|---|---|
| `vouch audit [--tail N] [--json]` | Read audit log |

### Export / Import
| Command | Description |
|---|---|
| `vouch export --out path.tar.gz` | Bundle KB into portable archive |
| `vouch export-check path.tar.gz` | Verify bundle integrity |
| `vouch import-check path.tar.gz` | Diff bundle against destination |
| `vouch import-apply path.tar.gz [--on-conflict ...]` | Apply bundle |

### Server
| Command | Description |
|---|---|
| `vouch serve [--transport stdio\|jsonl]` | Start MCP or JSONL server |

---

## MCP / JSONL Tool Surface

43 methods organized by intent:

### Read (unrestricted)
`kb_capabilities`, `kb_status`, `kb_search`, `kb_context`, `kb_read_page`, `kb_read_claim`, `kb_read_entity`, `kb_read_relation`, `kb_list_pages`, `kb_list_claims`, `kb_list_entities`, `kb_list_relations`, `kb_list_sources`, `kb_list_pending`

### Source Intake (not gated)
`kb_register_source`, `kb_register_source_from_path`, `kb_source_verify`

### Write (gated -> proposals)
`kb_propose_claim`, `kb_propose_page`, `kb_propose_entity`, `kb_propose_relation`

### Decisions
`kb_approve`, `kb_reject`

### Lifecycle (direct mutation, audited)
`kb_supersede`, `kb_contradict`, `kb_archive`, `kb_confirm`, `kb_cite`

### Sessions
`kb_session_start`, `kb_session_end`, `kb_crystallize`

### Maintenance
`kb_index_rebuild`, `kb_lint`, `kb_doctor`, `kb_audit`, `kb_export`, `kb_export_check`, `kb_import_check`, `kb_import_apply`

### Embeddings
`kb_reindex_embeddings`, `kb_dedup_scan`, `kb_eval_embeddings`, `kb_embeddings_stats`

---

## Embeddings Stack

Pluggable adapter architecture with auto-registration:

```
embeddings/
  base.py         -- Embedder protocol + registry (register / get_embedder)
  st_mpnet.py     -- all-mpnet-base-v2 (default, pip install vouch-kb[embeddings])
  st_minilm.py    -- all-MiniLM-L6-v2 (lighter alternative)
  fastembed_bge.py -- BGE via fastembed/ONNX (pip install vouch-kb[embeddings-fast])
```

### Search dispatch order
1. **Embedding** (semantic) -- sqlite-vec ANN or NumPy cosine fallback
2. **FTS5** -- BM25 over tokenized text
3. **Substring** -- brute-force text match (always available)
4. **Hybrid** -- RRF fusion of embedding + FTS5

### Advanced retrieval features
- **Query embedding cache** -- avoids re-encoding repeated queries
- **HyDE** -- hypothetical document expansion before encoding
- **Reranking** -- cross-encoder reranking of candidate hits
- **Dedup** -- cosine-based near-duplicate detection with audit logging
- **Evaluation** -- recall@k, MRR, NDCG via JSONL query sets

---

## Test Structure

### `tests/` (27 files)

| Test File | Covers |
|---|---|
| `test_storage.py` | KBStore CRUD, init, search, path safety |
| `test_cli.py` | CLI commands end-to-end (Click test runner) |
| `test_audit.py` | Audit log write/read, malformed line handling |
| `test_bundle.py` | Export/import, manifest integrity, path traversal |
| `test_capabilities.py` | Capabilities descriptor, method list sync |
| `test_context.py` | ContextPack assembly, budget enforcement |
| `test_health.py` | Status, lint findings, doctor, index rebuild |
| `test_index.py` | FTS5 indexing and search |
| `test_jsonl_server.py` | JSONL envelope handling, all methods |
| `test_onboarding.py` | Starter KB seeding |
| `test_sessions.py` | Session lifecycle, crystallize |
| `test_verify.py` | Source verification, drift detection |

### `tests/embeddings/` (12 files)

| Test File | Covers |
|---|---|
| `test_core.py` | Embedder registry, encode/decode |
| `test_cli.py` | Embedding CLI commands |
| `test_dedup.py` | Near-duplicate scanning |
| `test_fusion.py` | RRF fusion |
| `test_hyde.py` | HyDE query expansion |
| `test_integration.py` | End-to-end semantic search (marked `integration`) |
| `test_migration.py` | Model migration, backfill |
| `test_rerank.py` | Cross-encoder reranking |
| `test_scorer.py` | Retrieval evaluation metrics |
| `test_search.py` | Semantic search paths |
| `test_storage.py` | Embedding storage in SQLite |
| `conftest.py` / `_fakes.py` | Shared fixtures, fake embedder |

### Running tests
```bash
pytest                              # unit tests (fast, no model loading)
pytest -m integration               # integration tests (loads real embedding model)
```

---

## CI/CD

### `.github/workflows/`

| Workflow | Purpose |
|---|---|
| `ci.yml` | Lint (ruff), type check (mypy), test (pytest), coverage |
| `release.yml` | Build + publish to PyPI on release tags |
| `schema-check.yml` | Validate JSON schemas match pydantic models |

### Quality gates
- **Ruff** -- linter (E, F, I, B, UP, SIM, RUF rules, line-length 100)
- **Mypy** -- strict type checking (numpy/sentence-transformers/fastembed stubs ignored)
- **Pytest** -- unit tests by default; integration tests in separate job
- **Pre-commit** -- configured via `.pre-commit-config.yaml`

---

## Supporting Directories

| Directory | Purpose | Audience |
|---|---|---|
| `docs/` | User-facing documentation (9 guides + banner/demo) | End users, operators |
| `spec/` | Normative specification snapshots | Implementers, auditors |
| `schemas/` | 15 JSON Schemas (Draft 2020-12, generated from pydantic) | Tooling, validation |
| `proposals/` | VEP (Vouch Enhancement Proposals) | Protocol evolution |
| `templates/` | Copy-paste YAML/MD templates for every artifact type | Learning |
| `examples/` | Example KB layouts (tiny, decision-log) | Learning |
| `adapters/` | Per-runtime wiring guides (Claude Code, Cursor, Codex, Continue, generic MCP, JSONL shell) | Integration |
| `benchmarks/` | Benchmark fixtures and harness | Performance testing |
| `scripts/` | `gen_schemas.py` -- generate JSON schemas from models | Development |

---

## Dependencies

### Core (required)
| Package | Version | Purpose |
|---|---|---|
| pydantic | >=2.13.4, <3 | Domain models, validation |
| click | >=8.4.0, <9 | CLI framework |
| pyyaml | >=6, <7 | YAML serialization |
| mcp | >=1.0, <2 | MCP server SDK |

### Optional extras
| Extra | Packages | Purpose |
|---|---|---|
| `[embeddings]` | sentence-transformers, numpy | Semantic search (mpnet/minilm) |
| `[embeddings-fast]` | fastembed, onnxruntime, numpy, sqlite-vec | Semantic search (BGE, ONNX) |
| `[rerank]` | sentence-transformers | Cross-encoder reranking |
| `[dev]` | pytest, pytest-cov, mypy, ruff, types-pyyaml | Development |

---

## Codebase Statistics

| Metric | Count |
|---|---|
| Total Python LOC | ~8,400 |
| Source modules | 27 |
| Test modules | 27 |
| CLI commands | 30+ |
| MCP/JSONL tools | 43 |
| Pydantic models | 15 |
| JSON schemas | 15 |
| Embedding adapters | 3 |
| CI workflows | 3 |
| Documentation guides | 9 |

---

*Generated 2026-05-28 from source analysis of vouch v0.1.0 on branch `release/0.1.0`.*
