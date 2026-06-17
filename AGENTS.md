# Agents working with vouch

Entry document for any AI coding agent reading this repository — Cursor, Codex,
OpenClaw, Aider, Continue, JetBrains AI, an LLM fetching the raw URL, anything
that isn't Claude Code. (Claude Code reads [`CLAUDE.md`](./CLAUDE.md) instead;
the two files complement each other.)

If your task is to **work on the vouch codebase itself**, read this file and
then `CLAUDE.md`. If your task is to **use vouch from inside another
project**, read [`README.md`](./README.md) for install + concepts and
[`docs/getting-started.md`](./docs/getting-started.md) for the agent-side
loop.

If you're an **OpenClaw** plugin loader, the plugin manifest is at the repo
root: [`openclaw.plugin.json`](./openclaw.plugin.json). It declares vouch's
MCP wiring, the four slash commands, the trust boundary (write tools
review-gated, lifecycle ops audit-logged, remote-caller filesystem
confined), and the config schema (`kb_path`, `agent`, `transport` — no
secrets). No additional wiring is required to surface vouch's `kb.*`
surface inside an OpenClaw deployment.

## What vouch is, in one paragraph

Vouch is a git-native, review-gated knowledge base for LLM agents. Agents
propose writes via an MCP server (or a JSONL pipe); a human approves each
proposal with `vouch approve`. Approved artifacts land as YAML claims and
markdown pages under `.vouch/` — plain files that diff cleanly in PRs and
travel as a tarball bundle. The CLI is `vouch`; the PyPI distribution is
`vouch-kb`; supported Python versions are 3.11, 3.12, 3.13.

## Install (1 minute)

```bash
curl -fsSL https://raw.githubusercontent.com/vouchdev/vouch/main/install.sh | sh
```

Or, deterministically from a clone (the path you want when contributing):

```bash
git clone https://github.com/vouchdev/vouch.git
cd vouch
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

## Read in this order

1. **This file** — entry, install, trust boundary, common tasks.
2. [`CLAUDE.md`](./CLAUDE.md) — orientation for working on the repo:
   architecture, conventions, ship rules, voice. Read even if you aren't
   Claude Code; the conventions are universal.
3. [`README.md`](./README.md) — the user-facing pitch, install, quick start,
   full CLI surface, MCP / JSONL method list.
4. [`SPEC.md`](./SPEC.md) — the canonical protocol description: `.vouch/`
   layout, object model, `kb.*` method shapes, review-gate state machine.
   Authoritative when in doubt.
5. [`ROADMAP.md`](./ROADMAP.md) — what's planned for 0.2 and 0.3. Don't
   propose features that are already scoped here.

For everything else, see [`llms.txt`](./llms.txt) — the LLM-readable map of
every document in the repo.

## Trust boundary

Every `kb.*` write tool goes through the review gate. There is no "trusted
agent shortcut" except an opt-in `review.approver_role: trusted-agent` in
the KB's `config.yaml` (off by default). Concretely:

* `kb.propose_*` writes a YAML file to `.vouch/proposed/` — never to
  `.vouch/claims/` directly.
* `kb.approve` requires `approved_by != proposed_by` unless the trusted
  shortcut is on.
* Lifecycle ops (`kb.supersede`, `kb.contradict`, `kb.archive`,
  `kb.confirm`) mutate durable artifacts because they're metadata about
  reviewed knowledge, not new assertions. They still land an audit event.
* The MCP server, the JSONL server, and the CLI all share the same
  storage + proposals + audit code path. The web review-ui (PR #195,
  pending) is a *viewport*, not a parallel data path.

If you're proposing a fix that bypasses the gate, you're proposing the
wrong fix.

## Common agent tasks

### Use vouch from another project (the normal case)

```bash
cd /path/to/your/project
vouch init                          # create .vouch/
vouch install-mcp claude-code       # …or cursor, codex, continue, cline, windsurf, zed
# Restart the agent host. kb.* tools + slash commands now available.
```

`vouch install-mcp <host>` is manifest-driven: every supported host has a
single-file declaration under `adapters/<host>/install.yaml`. To add a
host, copy an existing manifest, point it at the host's config paths, and
open a PR.

### Contribute a bug fix or feature

```bash
git fetch origin main
git switch -c <type>/<topic> origin/main
# … edit …
.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src tests
git add <specific files>
git commit -m "<type>(<scope>): <≤72-char summary>"
git push -u origin <branch>
```

Commit type vocabulary: `feat | fix | refactor | test | docs | chore |
perf | ci | style | build | revert`. Body in lowercase prose, multi-line
ok, **no `Co-Authored-By: Claude` trailer** — see
[`CONTRIBUTING.md`](./CONTRIBUTING.md). CI runs the same three commands.

### Add a new `kb.*` method

Every kb method must be registered in **four** places — the
`test_capabilities` test catches drift:

1. `src/vouch/server.py` — the MCP tool function
2. `src/vouch/jsonl_server.py` — the matching handler + `HANDLERS` map entry
3. `src/vouch/capabilities.py` — append to `METHODS`
4. `src/vouch/cli.py` — the human-facing CLI mirror

Add a test in `tests/test_<method>.py` that asserts the JSONL envelope
shape (`{id, ok, result}` for success, `{id, ok: false, error}` for
failure).

### Add a new install-mcp host

```
adapters/<host>/
  install.yaml         # tier T1..T4 declarations
  .mcp.json            # T1 — MCP wire (project-local)
  CLAUDE.md.snippet    # T2 — fenced append into the host's instruction file
  .claude/commands/*   # T3 — slash commands (when the host supports them)
  .claude/settings.json # T4 — hooks + auto-allow lists
```

The writer is `src/vouch/install_adapter.py`. Strict YAML manifest
validation; semantic validation (does the `dst` path make sense for that
host?) is deliberately deferred.

## Before shipping

```bash
make check               # the convenience wrapper
# expanded:
.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src tests
```

`mypy src` is the gate that misses locally and fails CI; never push
without it.

## Privacy / disclosure

* Never paste a real Bearer token, GitHub PAT, or PyPI token into a
  commit, an issue, or a PR body. Use environment variables or
  `.env.local` (gitignored).
* Never propose claims with real customer names, internal URLs, or secret
  identifiers in a public KB. Use generic placeholders.
* Security issues go to the contact listed in [`SECURITY.md`](./SECURITY.md);
  do not file public issues for vulnerabilities.

## Hard rules

* **The review gate is non-negotiable.** Bypassing it is a rejected PR,
  not a feature.
* **Tests must be added with the change**, not after. CI red on a PR is a
  reviewer-blocking condition.
* **No `Co-Authored-By: <AI tool>` trailers** in commits — the user has
  been explicit about this.
* **No silent CHANGELOG omissions** when you add a user-visible feature —
  update `CHANGELOG.md` under `[Unreleased]` in the same PR.

If your task contradicts any rule above, stop and ask before continuing.
