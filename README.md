# vouch

**Git-native, review-gated knowledge base for LLM agents. MCP server + JSONL tool server + CLI.**

<p align="center">
  <img src="docs/banner.svg" alt="vouch — sessions auto-capture into a review-gated knowledge base: propose or capture → review → commit → retrieve" width="100%"/>
</p>

<p align="center">
  <a href="https://github.com/vouchdev/vouch/actions/workflows/ci.yml"><img src="https://github.com/vouchdev/vouch/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/vouch-kb/"><img src="https://img.shields.io/pypi/v/vouch-kb.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/vouch-kb/"><img src="https://img.shields.io/pypi/pyversions/vouch-kb.svg" alt="Python versions"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/vouchdev/vouch.svg" alt="MIT licensed"></a>
  <a href="https://x.com/vouch_dev"><img src="https://img.shields.io/badge/follow-%40vouch__dev-000000?logo=x&logoColor=white" alt="Follow @vouch_dev on X"></a>
</p>

> Agents should not start every session with amnesia — but they shouldn't get to write whatever they want either.

`vouch` is a knowledge base for LLM agents with an explicit **review gate**: agents *propose* writes; humans *approve* them with `vouch approve`. Approved artifacts are plain files on disk — YAML for claims, markdown for pages — so the KB lives in your repo, is reviewed in PRs, diffs cleanly, and can be exported as a portable bundle.

It also captures your Claude Code sessions automatically — each session's work is harvested and rolled up into a summary. But where the persistent-memory tools compress with an LLM and inject straight back, vouch's rollup is **mechanical (no LLM)** and the summary lands in the **same review gate**: nothing becomes durable memory until you approve it.

Still alpha — surface is small on purpose; expect breaking changes pre-1.0.

> **Built for Gittensor (SN74) miners.** Mining subnet 74 means landing merged PRs across a whitelist of repos that keeps shuffling — which means re-investigating each repo's codebase and merge bar every session your agent opens. vouch auto-captures what a session works out, you approve what's worth keeping, and the next session recalls it: less re-discovery, more merged PRs. → **[docs/gittensor.md](docs/gittensor.md)**

## Why this exists

Four opinionated choices distinguish vouch from the neighbours:

1. **The KB is a folder in your repo.** Git is your audit log, your backup, and your sync mechanism. PRs are your review UI.
2. **Writes require approval.** Agents file *proposals*; a human (or trusted approving agent) explicitly accepts them. `proposed/` is gitignored, so rejected drafts never pollute history.
3. **Claims must cite sources.** A claim without at least one Source / Evidence id is a validation error, not a warning. Sources are content-hashed; the same evidence registered twice de-duplicates.
4. **Sessions capture themselves — but stay gated.** With the Claude Code hooks installed, a `PostToolUse` hook harvests each tool call into a gitignored scratch buffer and a `SessionEnd` hook rolls it into one *pending* session-summary page. The harvest is automatic and the rollup is mechanical (no LLM) — but the commit still waits for your `approve`, and the next session starts from approved summaries via `vouch recall`.

## When to use vouch

Worth it when:

- **You mine Gittensor (SN74).** Emissions come from merged PRs across a whitelist that keeps shuffling, so your agent burns each session re-learning the same codebases and merge bars. vouch captures each session's findings for approval and recalls them next time — it stops re-deriving what it already knew and keeps targeting the repos that pay. See [docs/gittensor.md](docs/gittensor.md).
- **Multiple agents share a repo** (Claude Code + Cursor + a CI bot). Per-agent attribution in the audit log makes "which agent claimed what" answerable.
- **Sessions keep re-explaining the same context.** Curated, cited claims let new sessions start from your team's agreed answer instead of re-grepping — and vouch auto-captures each Claude Code session into a summary you can approve, so memory accrues without letting the agent write its own history.
- **You want decision records without the ADR ceremony.** `vouch crystallize` promotes a session's durable parts into proposals; one approve and they're permanent.
- **You'd review agent-stated facts the way you review agent-written code.** Vouch is a PR queue for claimed knowledge.
- **Compliance / audit trails matter.** Required citations + append-only audit log give you "who decided X, citing what, when" for free.

Skip it if:

- Solo hobby project where context fits in your head.
- Short-lived branches with no compounding context.
- Your team won't actually review proposals — without the gate, vouch is a worse note app.

## Install

```bash
# one-liner (Linux + macOS) — picks a Python, ensures pipx, installs vouch-kb
curl -fsSL https://raw.githubusercontent.com/vouchdev/vouch/main/install.sh | sh

# …or directly via pipx (vouch-kb on PyPI; the command stays `vouch`)
pipx install vouch-kb

# …or from the cloned repo, in a venv
pip install -e '.[dev]'
```

The one-liner is POSIX `sh`, never needs `sudo`, and detects an existing
Claude Code install to point you at the next step (`vouch install-mcp
claude-code`). Inspect it first if you'd like — it's [`install.sh`](install.sh)
at the repo root.

Or skip the install entirely and run the released container image
([`ghcr.io/vouchdev/vouch`](https://github.com/vouchdev/vouch/pkgs/container/vouch)),
bind-mounting the project root (the directory containing `.vouch/`) at
`/data`:

```bash
# stdio MCP server (the canonical surface — note -i)
docker run -i --rm -v "$PWD:/data" ghcr.io/vouchdev/vouch:latest

# any CLI command
docker run --rm -v "$PWD:/data" ghcr.io/vouchdev/vouch:latest status
```

## Running the tests

From a clone with the dev extras installed (`pip install -e '.[dev]'`):

```bash
# the full CI gate — lint + types + unit tests (exactly what CI runs)
make check                       # == ruff check + mypy src + pytest

# just the unit tests
python -m pytest tests/ -q --ignore=tests/embeddings

# a single module or test
python -m pytest tests/test_capture.py -q
python -m pytest tests/test_recall.py::test_digest_includes_approved_claim_and_page -q

# with coverage
make test-cov                    # term-missing + coverage.xml

# end-to-end smoke checks for the claude-code session hooks
make smoke-capture               # capture: observe → finalize → pending summary
make smoke-recall                # recall: approved knowledge injected at session start
```

The embedding-heavy tests live under `tests/embeddings/` and need the extra
`pip install -e '.[embeddings]'` (they run as a separate CI job); drop the
`--ignore` flag once installed.

## Quick start

```bash
# 1. set up a KB at your project root
vouch init

# 2. as agent, register evidence + propose claims (via MCP/JSONL server)
vouch serve                      # MCP over stdio  (Claude Code, Cursor, Codex)
vouch serve --transport jsonl    # newline-delimited JSON over stdin/stdout

# 3. as human, review and decide
vouch status                     # one-line summary
vouch pending                    # list pending proposals
vouch show <id>                  # full details
vouch approve <id>               # → durable artifact
vouch reject <id> --reason "..."
vouch expire --apply                  # optional: clear stale pending proposals

# 4. commit
git add .vouch/ && git commit -m "kb: approve auth-uses-jwt"
```

## 30-second tour

`vouch init` now creates a starter config plus one cited example claim, so
you can try the loop before wiring an agent:

```bash
mkdir vouch-demo && cd vouch-demo
vouch init
vouch status
vouch search agent
vouch cite vouch-starter-reviewed-knowledge
```

The starter claim is already durable and cites the starter source. Replace it
with your project's first real source and claim when you are ready.

### Automatic session capture

Once vouch's Claude Code hooks are installed, sessions capture *themselves* —
this is the loop the demo shows. A `PostToolUse` hook harvests each tool call
into a gitignored scratch buffer; at session end a `SessionEnd` hook rolls the
buffer plus a `git diff` backstop into **one pending "session summary" page** —
mechanically, no LLM, never auto-approved. You review and `vouch approve` it
like any other write, and the next session starts with it injected via `vouch
recall`. Passive harvest, human-gated commit, no re-explaining:

![vouch auto-capture demo](docs/demo.gif)

The full walkthrough with real output lives at [docs/example-session.md](docs/example-session.md); re-render the GIF from [docs/demo.tape](docs/demo.tape) with `vhs docs/demo.tape`.

Prefer reading to running? The [examples/](examples/) directory ships sample KBs as committed files, each with rendered screenshots of `vouch status`, `search`, `show`, `audit`, and `diff` against the fixture — see what the CLI returns before installing anything.

## Gittensor (SN74)

**Gittensor** — Bittensor subnet 74 — pays miners in TAO for landing *merged* pull requests into whitelisted open-source repos. Contributions are scored by code quality, each repo's allocation, and programming-language factors, and the whitelist itself shuffles as the subnet matures. So mining well means pointing a coding agent at a rotating set of target repos and landing mergeable PRs in the ones that pay.

The tax on that is re-investigation. Every target repo means (re)learning its architecture, its CI, its `CONTRIBUTING.md`, the maintainer's merge bar, and which past attempts got rejected and why. Across a dozen repos and a dozen sessions, your agent re-derives all of it from cold each time — time not spent landing PRs. vouch turns each session's findings into reviewed memory the next session recalls, so the investigation happens once:

```bash
cd acme-httpkit                   # a whitelisted target repo — Go, healthy allocation
vouch init                        # review-gated KB at .vouch/
vouch install-mcp claude-code     # wires the capture + recall hooks

# session 1: the agent maps the repo and works issue #212 (a pool leak).
#   the claude-code hooks auto-capture the work. you approve the durable
#   summary and file one cited claim: httpkit's merge bar.
vouch pending
vouch approve <id> --reason "accurate session summary"

# session 2: opens with `vouch recall`. the agent already knows the layout,
#   that merges need `make test` green + a changelog entry, and where #212's
#   fix and regression test landed. it skips re-discovery and takes the PR
#   to merge.
```

The KB compounds session over session: merge bars, rejected approaches, targeting notes. When the whitelist shuffles and httpkit's allocation drops, `vouch supersede` the claim that said it was worth targeting — your agent re-prioritizes toward a higher-allocation repo, and the decision stays cited and reviewed instead of lost in a Discord thread. Each session you approve makes the next one start further ahead; the walkthrough in [docs/gittensor.md](docs/gittensor.md) runs the loop with real output.

vouch reads **no** live signals — it never checks on-chain scores, verifies PATs, or submits weights (that's the `gitt` miner client). It's the reviewed record of what your agent already worked out, so it never works it out twice. Full miner walkthrough: **[docs/gittensor.md](docs/gittensor.md)**.

## Object model

```
Source     immutable input material (file, URL, transcript, commit, …)
           content-addressed by sha256

Evidence   span pointer into a Source (line range, timestamp, quote)

Claim      atomic durable assertion
           type: fact | decision | preference | workflow | observation | question | warning
           status: working | actionable | stable | contested | superseded | archived | redacted
           confidence: 0.0–1.0
           cites: list of Source / Evidence ids

Entity     typed thing (person | project | repo | concept | decision | system | …)
Relation   typed edge (uses | depends_on | supersedes | contradicts | implements | …)

Page       maintained markdown — entity write-up, decision record, session summary

Session    work block opened by an agent; bundles its proposals
AuditEvent append-only log record for every mutation
```

## File layout

After `vouch init`, your repo contains:

```
.vouch/
├── config.yaml                 # KB settings
├── .gitignore                  # ignores proposed/, state.db
├── audit.log.jsonl             # append-only audit (committed)
├── state.db                    # SQLite FTS5 index (derived; not committed)
├── claims/<id>.yaml            # reviewed claims (init seeds vouch-starter-*.yaml)
├── pages/<id>.md               # markdown pages with YAML frontmatter
├── sources/<sha>/{meta.yaml,content}
├── entities/<id>.yaml          # graph nodes
├── relations/<id>.yaml         # graph edges
├── evidence/<id>.yaml          # span pointers into sources
├── sessions/<id>.yaml          # agent session records
├── proposed/<id>.yaml          # pending (gitignored, local-only)
└── decided/<id>.yaml           # approved/rejected (committed for audit)
```

The files are the source of truth; `state.db` is a derivable cache (`vouch index` rebuilds it).

## CLI surface

```
vouch init                                  # set up .vouch/ at PATH
vouch install-mcp HOST [--tier T1-T4]       # wire MCP + capture/recall hooks into a host
vouch discover [--path P]                   # find the nearest .vouch/ root
vouch capabilities                          # emit the JSON capabilities descriptor
vouch status [--json]                       # KB counts + pending proposals
vouch stats [--days N] [--json]             # observability: queue, review rates, citations
vouch lint [--stale-days N]                 # user-actionable problems
vouch doctor                                # full sweep incl. source verification
vouch fsck                                  # deep consistency: indexes, lifecycle, decided
vouch migrate [--check] [--dry-run]         # upgrade .vouch/ format safely

vouch pending                               # list pending proposals
vouch review [--limit N] [--type KIND]      # guided proposal review queue
vouch show <proposal-id>                    # show pending proposal details
vouch read-claim <claim-id>                 # read an approved claim
vouch read-page <page-id>                   # read an approved page
vouch read-entity <entity-id>               # read an approved entity
vouch read-relation <relation-id>           # read an approved relation
vouch list-claims                           # list all approved claims
vouch list-pages                            # list all approved pages
vouch list-entities                         # list all approved entities
vouch list-relations                        # list all approved relations
vouch approve <proposal-id> [--reason ...]
vouch reject <proposal-id> --reason "..."
vouch expire [--apply] [--days N] [--json]   # GC stale pending proposals

vouch propose-claim --text ... --source ... [--type ...] [--confidence X]
vouch propose-page --title ... [--body -] [--claim ID ...]
vouch propose-entity --name ... --type ...
vouch propose-relation --from ID --rel uses --to ID

vouch source add PATH [--title ...] [--url ...]
vouch source verify [--fail-on-issue]

vouch supersede OLD_ID NEW_ID
vouch contradict CLAIM_A CLAIM_B
vouch archive CLAIM_ID
vouch confirm CLAIM_ID
vouch cite CLAIM_ID

vouch session start [--task ...] [--note ...]
vouch session end SESSION_ID
vouch crystallize SESSION_ID [--no-page]

vouch recall                                # digest of approved knowledge for session-start injection
vouch capture observe|finalize|finalize-all|banner   # hook-driven session capture (claude code)

vouch search QUERY [--limit N]
vouch context TASK [--max-chars N] [--min-items N] [--require-citations]
vouch index
vouch audit [--tail N] [--json]

vouch export --out path.tar.gz
vouch export-check path.tar.gz
vouch import-check path.tar.gz
vouch import-apply path.tar.gz [--on-conflict skip|overwrite|fail]
vouch sync-check PATH_OR_BUNDLE
vouch sync-apply PATH_OR_BUNDLE [--on-conflict fail|skip|propose]

vouch serve [--transport stdio|jsonl]
```

## MCP tools / JSONL methods (same surface, two transports)

Read (unrestricted): `kb.capabilities`, `kb.status`, `kb.search`, `kb.context`, `kb.read_{page,claim,entity,relation}`, `kb.list_{pages,claims,entities,relations,sources,pending}`

Source intake (not gated — evidence is harmless and de-duplicates): `kb.register_source`, `kb.register_source_from_path`, `kb.source_verify`

Write (gated → produce proposals): `kb.propose_{claim,page,entity,relation}` (with `dry_run:true` for preview-only)

Decisions: `kb.approve`, `kb.reject` (host trust required)

Lifecycle (metadata about reviewed knowledge — direct mutation, audited): `kb.supersede`, `kb.contradict`, `kb.archive`, `kb.confirm`, `kb.cite`

Sessions: `kb.session_start`, `kb.session_end`, `kb.crystallize`

Maintenance: `kb.index_rebuild`, `kb.lint`, `kb.doctor`, `kb.audit`, `kb.export`, `kb.export_check`, `kb.import_check`, `kb.import_apply`

## Wiring into Claude Code

In your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "vouch": {
      "command": "vouch",
      "args": ["serve"],
      "env": { "VOUCH_AGENT": "claude-code" }
    }
  }
}
```

`VOUCH_AGENT` is recorded as `proposed_by` and as the actor on every audit event, so multi-agent setups can attribute writes correctly.

The `.mcp.json` above wires the MCP server only. To also turn on **automatic session capture** and start-of-session **recall**, install the Claude Code hooks:

```bash
vouch install-mcp claude-code
```

Its `.claude/settings.json` (tier T4) registers a `PostToolUse` hook (`vouch capture observe`), a `SessionEnd` hook (`vouch capture finalize`), and a `SessionStart` hook that runs `vouch recall` and nudges any pending captured summaries. Capture only ever files *pending* proposals — the review gate holds. The full loop is walked in [docs/example-session.md](docs/example-session.md).

## Running vouch as an OpenClaw plugin

Vouch ships an [OpenClaw](https://github.com/dripsmvcp/openclaw) plugin at the
repo root — [`openclaw.plugin.json`](openclaw.plugin.json) plus a small
[`package.json`](package.json) that points the loader at the JS entry module.
Install the repo as a linked plugin and OpenClaw picks up two things
automatically:

* the **vouch context engine** — registered as `vouch` and auto-bound to
  `plugins.slots.contextEngine` on install, it injects cited KB context
  (retrieval + salience reflex + hot memory) into every agent turn, and
* the **four skills / slash commands** (`/vouch-recall`, `/vouch-status`,
  `/vouch-resolve-issue`, `/vouch-propose-from-pr`).

```bash
openclaw plugins install --link /path/to/vouch
# the kb.* MCP tool surface is deployment config, same shape as every host:
openclaw mcp add vouch -- vouch serve
```

The `configSchema` exposes only `kb_path` and `agent` — no API keys, no
secrets; vouch is local-first. The trust boundary (confined filesystem for
remote callers, review-gated writes, audit-logged lifecycle ops) ships as
project-local policy via `vouch install-mcp openclaw` — see
[`adapters/openclaw/`](adapters/openclaw/).

## JSONL request/response shape

The JSONL transport reads one envelope per line on stdin, writes one per line on stdout:

```jsonl
{"id":"r1","method":"kb.search","params":{"query":"jwt","limit":5}}
{"id":"r1","ok":true,"result":[{"kind":"claim","id":"auth-uses-jwt","snippet":"…","score":1.2,"backend":"fts5"}]}
```

Errors come back with `ok:false` and a structured `error.code` (`method_not_found`, `missing_param`, `invalid_request`, `internal_error`).

Every successful `kb.*` result that is object-shaped carries read-only trust metadata so clients can detect remote confinement:

```json
{
  "id": "r1",
  "ok": true,
  "result": {
    "backend": "fts5",
    "hits": [],
    "_meta": {
      "vouch_trust": {
        "remote": false,
        "caller_kind": "jsonl",
        "auth_subject": null
      }
    }
  }
}
```

| Transport | `remote` | `caller_kind` | `auth_subject` |
|-----------|----------|---------------|----------------|
| JSONL stdio | `false` | `jsonl` | `null` |
| HTTP `/rpc` | `true` | `jsonl_http` | bearer fingerprint when authenticated |
| MCP stdio | `false` | `mcp_stdio` | `null` |
| HTTP `/mcp` | `true` | `mcp_http` | bearer fingerprint when authenticated |
| CLI `--json` | `false` | `cli` | `null` |

The block is server-attached metadata — client mutations are ignored. Array-shaped read results (e.g. `kb.list_claims`) pass through unchanged; trust rides on dict-shaped responses only (#233).

## Portable bundles

```bash
vouch export --out kb.tar.gz                     # tar.gz + manifest.json with per-file sha256
vouch export-check kb.tar.gz                     # verify every file against the manifest
vouch import-check kb.tar.gz                     # diff against destination — new / conflict / identical
vouch import-apply kb.tar.gz --on-conflict skip  # apply (default skip; never destructive without overwrite)
```

`proposed/`, `state.db`, and `audit.log.jsonl` are excluded from bundles — only committable artifacts travel.

## Compared to neighbours

| | mem0 / Letta | LLM-Wiki tools | **vouch** |
|---|---|---|---|
| Knowledge lives in | a service | filesystem | your **repo** |
| Review of writes | none | none | **explicit `approve`** |
| Session auto-capture | via LLM extraction | no | **yes — gated** |
| Summaries need an LLM | yes | — | **no (mechanical)** |
| Evidence required | no | optional | **enforced** |
| Per-agent attribution | partial | none | **yes** (audit log) |
| Graph (entities + relations) | no | no | **yes** |
| FTS5 search | no | varies | **yes** |
| Portable bundle | no | no | **yes** |
| Transports | SDK / HTTP | none | **MCP + JSONL** |

## What ships today

| Area | Current support |
|------|-----------------|
| Knowledge base | `.vouch/` folder, YAML claims/entities/relations/evidence/sessions, markdown pages with frontmatter, JSONL audit log, content-addressed sources |
| CLI | `init`, `install-mcp`, `discover`, `capabilities`, `status`, `lint`, `doctor`, `fsck`, `pending`, `show`, `approve`, `reject`, `propose-{claim,page,entity,relation}`, `source add`, `source verify`, `supersede`, `contradict`, `archive`, `confirm`, `cite`, `session {start,end}`, `crystallize`, `capture {observe,finalize,finalize-all,banner}`, `recall`, `search`, `context`, `index`, `audit`, `export`, `export-check`, `import-check`, `import-apply`, `serve` |
| Tool servers | MCP over stdio + JSONL over stdin/stdout, same `kb.*` surface across both transports, capabilities + knowledge-capability descriptor |
| Schemas | 13 JSON Schemas (Draft 2020-12) generated from pydantic in [schemas/](schemas/), plus hand-maintained `bundle.manifest` and `jsonl-envelope` schemas |
| Write safety | review-gated writes via [proposed/](spec/review-gate.md), `dry_run:true` previews, host trust required for `approve`/`reject`, atomic exclusive-create storage, path-traversal blocked on source intake and bundle import |
| Session capture | Claude Code hooks harvest each session (`PostToolUse` → `vouch capture observe`) into a gitignored scratch buffer; `SessionEnd` rolls it up mechanically (no LLM) into one review-gated session-summary page; `SessionStart` injects approved knowledge via `vouch recall` and nudges pending summaries. Never auto-approves |
| Retrieval | `retrieval.backend` in `config.yaml` selects the path: `auto` (default — embedding → FTS5 → substring), `embedding`, `fts5`, or `substring`. Semantic backends (`all-mpnet-base-v2`, `MiniLM-L6`, fastembed-BGE) ship behind install extras; `auto` degrades to FTS5 when they aren't installed. Context packs with citations + quality gate |
| Lifecycle | `supersede`, `contradict`, `archive`, `confirm`, `cite` — direct mutations, all audited |
| Portability | tar.gz bundles with per-file sha256 `manifest.json`, `export-check`, `import-check`, `import-apply` with skip/overwrite/fail conflict modes |
| Audit | append-only `audit.log.jsonl`, per-event actor (`VOUCH_AGENT`), object ids, dry-run flag, reversible flag |
| Adapters | Claude Code wiring documented via `.mcp.json` + `VOUCH_AGENT` env; per-runtime adapter templates not yet shipped |
| Validation | pytest suite (storage, FTS5, audit, source-verify, review-gate, bundle, JSONL), ruff + mypy gates, GitHub CI |
| Specification | dated snapshots under [spec/](spec/), JSON Schemas in [schemas/](schemas/), generator script at [scripts/gen_schemas.py](scripts/gen_schemas.py) |

## Status

Pre-1.0. What's *not* in this implementation: per-runtime adapter templates, benchmark fixtures, multi-agent sync, scopes beyond a single field on Claim/Source. If a hole matters to you, file an issue.

## License

MIT.
