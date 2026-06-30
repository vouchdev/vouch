# examples/

Real-shaped sample KBs that show what vouch looks like in use.

There are two kinds of example here. **Snapshots** are self-contained
`.vouch/`-style trees you copy and poke at. **Runnable flows** and
**playbooks** are scripts (`run.sh`) that drive the real CLI/JSONL surface
against a throwaway KB and clean up after themselves.

## Snapshot KBs

Each snapshot subdirectory is a self-contained `.vouch/`-style tree (without
the leading dot — `vouch/`, so it's visible in the GitHub UI). Copy the
contents into a real `.vouch/` directory at your project root to play with
the data.

| Example | Topic | Size |
|---|---|---|
| [tiny/](tiny/) | A four-claim KB about a hypothetical auth design | 4 claims, 1 page, 2 sources |
| [decision-log/](decision-log/) | What a team's decision log looks like as vouch claims | 6 claims, 3 pages |
| [level-0-markdown-wiki/](level-0-markdown-wiki/) | Conformance level 0: pages only, no claims, no graph — a plain markdown wiki | 3 pages, 0 claims |
| [level-3-graph/](level-3-graph/) | Conformance level 3: a full claim + entity + relation graph with provenance | 5 claims, 3 pages, 6 entities, 6 relations |

## Trying a snapshot

```bash
mkdir my-test && cd my-test
git init
cp -r /path/to/vouch/examples/tiny/vouch ./.vouch
vouch status
vouch search auth
vouch pending           # empty — examples ship as already-approved
```

The snapshot KBs ship with everything already in `claims/`, `pages/`,
etc. — they're "post-approval" snapshots. There are no `proposed/`
entries because proposals are local-only by nature.

## Runnable flows

Each of these is a script that builds a throwaway KB in `$(mktemp -d)`,
runs the real surface against it, asserts the end state, and cleans up on
exit. Nothing touches your real `.vouch/`. Run any of them directly; set
`VOUCH=/path/to/vouch` to point at a specific binary (default is `vouch` on
your `PATH`).

| Example | What it shows | Run |
|---|---|---|
| review-gate-flow | the review gate end to end: register, propose, reject one, approve the other, see it in search | `./examples/review-gate-flow/run.sh` |
| review-gate-dry-run-preview | preview a write with `dry_run:true`, file a real pending proposal, and prove only `approve` makes it durable | `./examples/review-gate-dry-run-preview/run.sh` |
| jsonl-quickstart | the shortest adapter contract for a new host: discovery → status → search → context over JSONL | `./examples/jsonl-quickstart/run.sh` |
| search-backends | the same query through `fts5` / `substring` / `embedding` / `hybrid` backends, plus the `--min-score` floor | `./examples/search-backends/run.sh` |
| context-and-synthesize | budgeted ContextPacks and cited answers, with the require-citations quality gate | `./examples/context-and-synthesize/run.sh` |
| browse-and-read | the read side: every `list_*` enumerator, every `read_*` fetcher, and `cite` | `./examples/browse-and-read/run.sh` |
| propose-family | all four proposal entry points plus path-based source registration, left pending | `./examples/propose-family/run.sh` |
| lifecycle-supersede-contradict | how a claim ages: supersede, contradict, archive, confirm | `./examples/lifecycle-supersede-contradict/run.sh` |
| lifecycle-expire-and-reject-extracted | queue hygiene: stale-proposal expiry and bulk reject of auto-extracted edges, both dry-run-first | `./examples/lifecycle-expire-and-reject-extracted/run.sh` |
| sessions-and-crystallize | a full agent run: session start, volunteer context, end, then crystallize the whole session | `./examples/sessions-and-crystallize/run.sh` |
| graph-and-provenance | walk the typed graph four ways: neighbors, why, trace, impact | `./examples/graph-and-provenance/run.sh` |
| provenance-graph-export | render the provenance DAG (dot / mermaid) and rebuild its derived cache | `./examples/provenance-graph-export/run.sh` |
| maintenance-health | the operator toolkit: index rebuild, lint, doctor, source verify, stats — against a deliberately broken citation | `./examples/maintenance-health/run.sh` |
| embeddings-suite | the four `[embeddings]`-extra methods (reindex, dedup-scan, eval, stats), skipping gracefully when the extra is absent | `./examples/embeddings-suite/run.sh` |
| export-import-bundle | move a reviewed KB between trees: export, export-check, import-check, import-apply | `./examples/export-import-bundle/run.sh` |
| audit-timeline | read the authoritative audit log back as a live, actor-attributed timeline | `./examples/audit-timeline/run.sh` |
| scoping-and-visibility | the same query and audit log narrowed per viewer (`--project` / `--agent`) | `./examples/scoping-and-visibility/run.sh` |

### Playbooks

These exercise vouch features that spawn external coding agents (claude +
codex), reach the network, or run an unbounded watch loop — unsafe to run
unattended in an examples harness. Each `run.sh` runs only the safe,
read-only legs and prints the real command (with requirements) instead of
executing the dangerous part.

| Example | What it shows | Run |
|---|---|---|
| playbook-install-mcp | installing vouch into a host (Claude Code, Cursor, …) across the T1..T4 adoption tiers, then the discovery calls a host makes first | `./examples/playbook-install-mcp/run.sh` |
| playbook-vault-sync | bidirectional Obsidian vault sync — mirror approved pages/claims out, turn vault edits into review-gated proposals back | `./examples/playbook-vault-sync/run.sh` |
| playbook-dual-solve | `vouch dual-solve`: claude and codex each fix the same issue in their own worktree; the chosen rationale lands as a pending proposal | `./examples/playbook-dual-solve/run.sh` |
| playbook-auto-pr | `vouch auto-pr`: source issues, bootstrap a contribution skill, fix-and-cross-verify, open a PR only when the repo's gate is green | `./examples/playbook-auto-pr/run.sh` |

## Method-coverage matrix

Every `kb.*` method, mapped to the example(s) that demonstrate it. This is
the auditable record that the surface stays exercised — when you add a
method, add an example and a row here.

| Method | Demonstrated by |
|---|---|
| `kb.capabilities` | jsonl-quickstart, playbook-install-mcp, playbook-auto-pr |
| `kb.status` | review-gate-flow, jsonl-quickstart, export-import-bundle, scoping-and-visibility, playbook-install-mcp |
| `kb.search` | review-gate-flow, jsonl-quickstart, search-backends, context-and-synthesize, scoping-and-visibility |
| `kb.context` | jsonl-quickstart, context-and-synthesize, scoping-and-visibility |
| `kb.synthesize` | context-and-synthesize |
| `kb.stats` | search-backends, maintenance-health |
| `kb.register_source` | review-gate-flow, audit-timeline |
| `kb.register_source_from_path` | propose-family |
| `kb.propose_claim` | review-gate-flow, review-gate-dry-run-preview, propose-family, audit-timeline, playbook-dual-solve |
| `kb.propose_entity` | propose-family |
| `kb.propose_relation` | propose-family |
| `kb.propose_page` | propose-family, playbook-vault-sync |
| `kb.list_pending` | review-gate-flow, review-gate-dry-run-preview, propose-family, lifecycle-expire-and-reject-extracted, sessions-and-crystallize, playbook-vault-sync, playbook-dual-solve |
| `kb.approve` | review-gate-flow, review-gate-dry-run-preview, audit-timeline, playbook-dual-solve |
| `kb.reject` | review-gate-flow, audit-timeline |
| `kb.list_pages` | browse-and-read, level-0-markdown-wiki |
| `kb.list_claims` | browse-and-read |
| `kb.list_entities` | browse-and-read, level-3-graph |
| `kb.list_relations` | browse-and-read, level-3-graph |
| `kb.list_sources` | browse-and-read |
| `kb.read_page` | browse-and-read, level-0-markdown-wiki, playbook-vault-sync |
| `kb.read_claim` | review-gate-dry-run-preview, browse-and-read, lifecycle-supersede-contradict |
| `kb.read_entity` | browse-and-read |
| `kb.read_relation` | browse-and-read |
| `kb.cite` | browse-and-read |
| `kb.supersede` | lifecycle-supersede-contradict |
| `kb.contradict` | lifecycle-supersede-contradict |
| `kb.archive` | lifecycle-supersede-contradict |
| `kb.confirm` | lifecycle-supersede-contradict |
| `kb.expire` | lifecycle-expire-and-reject-extracted |
| `kb.reject_extracted` | lifecycle-expire-and-reject-extracted |
| `kb.session_start` | sessions-and-crystallize |
| `kb.volunteer_context` | sessions-and-crystallize |
| `kb.session_end` | sessions-and-crystallize |
| `kb.crystallize` | sessions-and-crystallize |
| `kb.neighbors` | graph-and-provenance, level-3-graph |
| `kb.why` | graph-and-provenance |
| `kb.trace` | graph-and-provenance |
| `kb.impact` | graph-and-provenance |
| `kb.graph_export` | provenance-graph-export, level-3-graph |
| `kb.provenance_rebuild` | provenance-graph-export |
| `kb.index_rebuild` | maintenance-health |
| `kb.lint` | maintenance-health |
| `kb.doctor` | maintenance-health |
| `kb.source_verify` | maintenance-health |
| `kb.reindex_embeddings` | embeddings-suite |
| `kb.dedup_scan` | embeddings-suite |
| `kb.eval_embeddings` | embeddings-suite |
| `kb.embeddings_stats` | embeddings-suite |
| `kb.export` | export-import-bundle |
| `kb.export_check` | export-import-bundle |
| `kb.import_check` | export-import-bundle |
| `kb.import_apply` | export-import-bundle |
| `kb.audit` | audit-timeline, scoping-and-visibility |

All 54 `kb.*` methods are covered. Nothing under **Not yet covered**.

## Anti-examples

We deliberately don't ship a "200k claims, every entity in the world"
example. Those are benchmark fixtures, not learning material. See
[benchmarks/](../benchmarks/) for synthesizing those.
