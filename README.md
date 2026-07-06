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

The destination is the one [Andrej Karpathy's llm-wiki idea file](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) sketches: stop using LLMs as search engines that rediscover your documents on every question — use them as tireless knowledge engineers that compile, cross-reference, and maintain a living wiki, while humans curate and think. vouch is that idea with the write path made trustworthy. `vouch compile` has an LLM draft the topic pages, but every page cites approved claims, every `[claim: …]` citation is machine-verified before the draft is filed, and the drafts pass through the same review gate as every other write. The LLM compiles; the human approves; the wiki compounds.

> **Built for Gittensor (SN74) miners.** Mining subnet 74 means landing merged PRs across a whitelist of repos that keeps shuffling — which means re-investigating each repo's codebase and merge bar every session your agent opens. vouch auto-captures what a session works out, you approve what's worth keeping, and the next session recalls it: less re-discovery, more merged PRs. → **[docs/gittensor.md](docs/gittensor.md)**

## Watch it work (110 seconds)

[![vouch demo — capture, summarize, approve, compile, recall](docs/img/how-it-works-preview.gif)](docs/vouch-how-it-works.mp4)

**capture → summarize → approve → compile → recall.** Captured live from the review console, no mockups — the preview above is muted and 3× speed; the full cut is **[▶ docs/vouch-how-it-works.mp4](docs/vouch-how-it-works.mp4)**. A Claude Code session captures itself, an LLM summarizes what the session *meant*, a human approves it at the gate, **`vouch compile`** distills the approved claims into cited topic pages (every `[claim: …]` citation machine-verified, still gated), and the film closes on real `vouch recall` output — the wiki the video just built, injected into the next session's first turn.

## Why this exists

Four opinionated choices distinguish vouch from the neighbours:

1. **The KB is a folder in your repo.** Git is your audit log, your backup, and your sync mechanism. PRs are your review UI.
2. **Writes require approval.** Agents file *proposals*; a human (or trusted approving agent) explicitly accepts them. `proposed/` is gitignored, so rejected drafts never pollute history.
3. **Claims must cite sources.** A claim without at least one Source / Evidence id is a validation error, not a warning. Sources are content-hashed; the same evidence registered twice de-duplicates.
4. **Sessions capture themselves — but stay gated.** With the Claude Code hooks installed, a `PostToolUse` hook harvests each tool call into a gitignored scratch buffer and a `SessionEnd` hook rolls it into one *pending* session-summary page. The harvest is automatic and the rollup is mechanical (no LLM) — but the commit still waits for your `approve`, and the next session starts from approved summaries via `vouch recall`.

## When to use vouch

Worth it when:

- **Multiple agents share a repo** (Claude Code + Cursor + a CI bot). Per-agent attribution in the audit log makes "which agent claimed what" answerable.
- **Sessions keep re-explaining the same context.** Curated, cited claims let new sessions start from your team's agreed answer instead of re-grepping.
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
```

The one-liner is POSIX `sh` and never needs `sudo` — inspect
[`install.sh`](install.sh) first if you'd like.

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

Once vouch's Claude Code hooks are installed, sessions capture *themselves*:
each tool call is harvested into a gitignored scratch buffer and rolled up at
session end into **one pending "session summary" page** — mechanically, no
LLM, never auto-approved. You review and `vouch approve` it like any other
write, and the next session starts with it injected via `vouch recall`:

![vouch auto-capture demo](docs/demo.gif)

The full walkthrough with real output lives at
[docs/example-session.md](docs/example-session.md); the [examples/](examples/)
directory ships sample KBs with rendered CLI output so you can see what vouch
returns before installing anything.

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

The daily loop:

```text
vouch init                                  # set up .vouch/ at the project root
vouch install-mcp HOST [--tier T1-T4]       # wire MCP + capture/recall hooks into a host
vouch serve [--transport stdio|jsonl]       # the agent-facing tool server

vouch status | pending | review | show <id> # inspect the queue
vouch approve <id> [--reason ...]           # the gate
vouch reject <id> --reason "..."

vouch propose-claim|propose-page|...        # human mirror of the kb.propose_* tools
vouch search QUERY | context TASK | recall  # retrieval
vouch compile [--dry-run]                   # llm-wiki ingest: draft topic pages from claims
vouch export | import-check | import-apply  # portable bundles
```

The full surface — lifecycle ops, sessions, sources, maintenance,
migration — is listed by `vouch --help` and machine-readably by
`vouch capabilities`.

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

Every dict-shaped `kb.*` result also carries a server-attached
`_meta.vouch_trust` block (`remote`, `caller_kind`, `auth_subject`) so clients
can detect remote confinement — see [SPEC.md](SPEC.md) for the full contract.

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

## Status

Pre-1.0 — the surface is small on purpose; expect breaking changes. What's
*not* in this implementation: benchmark fixtures, multi-agent sync, scopes
beyond a single field on Claim/Source. If a hole matters to you, file an
issue. Development setup and the test gate live in
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT.
