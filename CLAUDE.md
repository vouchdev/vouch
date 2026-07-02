# CLAUDE.md — orientation for Claude Code working on vouch

This file is read automatically by Claude Code when you open the vouch
repository. It exists to make a fresh session productive without you
having to re-onboard it every time.

If you're working *with* vouch from inside a different project (proposing
claims, approving them), read [`README.md`](./README.md) instead. This
file is for working *on* vouch — fixing a bug, adding a feature,
shipping a release.

For any non-Claude-Code agent — start with [`AGENTS.md`](./AGENTS.md).

## North star

Vouch is a knowledge base where every write goes through a review gate.
That's the load-bearing invariant. Every other design choice — files on
disk, append-only audit log, manifest-driven adapters, thin viewports
over the storage layer — is downstream of "writes must be reviewed."

If a PR adds a parallel data path that bypasses `proposals.approve()`,
the PR is wrong. Push back. Find the right factoring.

## Architecture (90 seconds)

```
                        ┌──────────────┐
   Claude Code ─MCP──▶  │              │
   Cursor      ─MCP──▶  │  server.py   │
   Codex       ─MCP──▶  │  jsonl_      │ ─┐
   CLI human   ──────▶  │  server.py   │  │
                        │  cli.py      │  │
                        └──────┬───────┘  │
                               │           │
                               ▼           │
                       ┌──────────────┐    │
                       │ proposals.py │    │   review gate
                       │ lifecycle.py │    │   (kb.approve etc.)
                       └──────┬───────┘    │
                              │            │
                              ▼            │
                       ┌──────────────┐    │
                       │  storage.py  │ ◀──┘
                       │  audit.py    │
                       │  index_db.py │
                       └──────┬───────┘
                              │
                              ▼
              .vouch/  ─── filesystem (yaml + md + jsonl)
                       ─── state.db (FTS5 + optional embeddings, derived)
```

Three rules that fall out of the layout:

1. **`src/vouch/storage.py` is pure I/O.** No business logic. If you find
   yourself doing scope filtering or status transitions inside `put_claim`,
   you're in the wrong file.
2. **All three surfaces (MCP, JSONL, CLI) call the same `proposals.*` and
   `lifecycle.*` functions.** Drift between surfaces is the most common
   contributor mistake. `test_capabilities` enforces method-list parity;
   you still need to keep behaviour aligned by reading the existing
   handlers before you add a new one.
3. **The audit log is the only authoritative history.** `decided/` is the
   queryable summary; `audit.log.jsonl` is the legally-authoritative event
   stream. Both are committed. Never edit either by hand.

## Build + test + ship

```bash
# from a clone
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# the CI gate — exactly what .github/workflows/ci.yml runs
.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src tests

# the convenience wrapper
make check
```

`mypy src` is the gate that gets missed locally and turns CI red.

For the embedding-heavy tests (separate job in CI): `pip install
-e '.[embeddings]'` then drop the `--ignore=tests/embeddings` flag.

## Ship a feature branch

```bash
git fetch origin main
git switch -c <type>/<topic> origin/main
# … work …
make check
git add <files-by-name>          # never `git add -A` — leaks .claude/, web/, etc.
git commit -m "<type>(<scope>): <≤72-char summary>

multi-line lowercase body explaining the why.
no Co-Authored-By trailer."
git push -u origin <branch>
```

If there's a pre-existing `M src/vouch/storage.py` edit in the working
tree on `release/0.1.0` (the user's WIP), stash it first:

```bash
git stash push -m "preserve user wip" src/vouch/storage.py
```

…work on the feature branch, then `git stash pop` after switching back.
The `.claude/skills/vouch-ship/SKILL.md` skill encodes this dance.

## Commit messages

Conventional commits, enforced by a pre-commit hook:

```
<type>(<scope>): <summary, lowercase, ≤72 chars>

<optional body — lowercase prose, multiple paragraphs ok>
```

Types: `feat | fix | refactor | test | docs | chore | perf | ci | style |
build | revert`. Scope optional. Anchor voice against `git log --oneline
-10` before drafting a new one.

**No `Co-Authored-By: <AI tool>` trailer.** The user has been explicit
about this; it's checked in PR review.

## Conventions you'll trip on otherwise

* **Lowercase prose** in PR bodies, commit bodies, and review comments.
  Match the existing voice.
* **No inline `##` headers inside a postable PR-review comment block** —
  reviews are 4-6 short paragraphs in vouch's house style. See
  `.claude/skills/vouch-pr-comment/SKILL.md` if it's installed.
* **No "we" / "let's" marketing tone in code comments.** Comments
  explain why, not what.
* **Specific files only when staging.** `git add -A` will pull in
  `.claude/`, `web/`, `proposed-features.md`, etc. that are local scratch.
* **Stash + worktree** for doc-only changes: branch off
  `origin/main` in `/tmp/vouch-<topic>-wt`, do the work, push, remove the
  worktree.

## Where things live

| Concern | File |
|---|---|
| MCP tool surface | `src/vouch/server.py` |
| JSONL handler map | `src/vouch/jsonl_server.py` |
| CLI commands | `src/vouch/cli.py` |
| Pure file I/O | `src/vouch/storage.py` |
| Proposal lifecycle | `src/vouch/proposals.py` |
| Claim lifecycle (supersede, etc.) | `src/vouch/lifecycle.py` |
| Audit log writer | `src/vouch/audit.py` |
| Pydantic models | `src/vouch/models.py` |
| Capabilities + method list | `src/vouch/capabilities.py` |
| Context-pack builder | `src/vouch/context.py` |
| SQLite FTS5 + embeddings | `src/vouch/index_db.py` |
| Sessions | `src/vouch/sessions.py` |
| Manifest-driven adapter writer | `src/vouch/install_adapter.py` |
| Web review-ui (when PR #195 lands) | `src/vouch/web/` |
| OpenClaw plugin manifest | `openclaw.plugin.json` + `package.json` (repo root) |
| Claude Code / Cursor / etc. install templates | `adapters/<host>/` |

Tests mirror module names (`tests/test_<module>.py`); the convention is
strict.

## The OpenClaw plugin packaging

Two files make the vouch repo loadable directly as an OpenClaw plugin
(`openclaw plugins install --link <repo>`):

* [`openclaw.plugin.json`](./openclaw.plugin.json) — the manifest the
  loader parses: `id` + `configSchema` (required), `kind: context-engine`,
  `version`, and `skills` (directories under `adapters/openclaw/skills/`,
  one SKILL.md each; OpenClaw publishes them as skills *and* slash
  commands).
* [`package.json`](./package.json) — loader-facing only: the
  `openclaw.extensions` pointer at the JS entry module and the
  `openclaw.compat.pluginApi` floor. The Python package stays in
  `pyproject.toml`.

Invariants, all enforced by `tests/test_openclaw_plugin_manifest.py`:

* **One id everywhere.** The manifest `id`, the Python `ENGINE_ID`, and the
  entry module's export id are all `vouch`. OpenClaw's installer auto-binds
  `plugins.slots.contextEngine` to the *plugin* id and resolves that slot by
  *engine* id — if they ever diverge, the engine is quarantined at resolve
  time and OpenClaw silently falls back to its legacy engine.
* **Version in three places.** `pyproject.toml`, `openclaw.plugin.json`,
  and `package.json` must agree.
* **Skills mirror the claude-code commands.** Each
  `adapters/openclaw/skills/<name>/SKILL.md` body must match the
  corresponding `adapters/claude-code/.claude/commands/<name>.md` body.
* **No dead dialect fields.** `mcpServers`, `contracts`, `family`,
  `shared_deps`, `openclaw.*` in the manifest are silently ignored by
  current loaders — the sync test rejects them. The kb.* MCP server is
  deployment config (`openclaw mcp add vouch -- vouch serve`); the trust
  boundary lives in `adapters/openclaw/policy.json`.

`tests/test_openclaw_plugin_load_real.py` is the live gate: with the
`openclaw` CLI on PATH it links the repo into an isolated profile and
asserts import, engine registration, slot auto-bind, skill publication,
and a clean `plugins doctor`. It skips (does not fail) where the CLI is
absent, e.g. GitHub CI.

Keep the manifest small. Anything that would require a runtime decision
(which kb to use, whose audit log to write to) belongs in the deployment's
own config, not in the plugin packaging.

## When you add a new `kb.*` method

Four registration sites — `test_capabilities` will fail if you miss one:

1. **MCP tool** in `src/vouch/server.py` (decorated with `@mcp.tool()`)
2. **JSONL handler** in `src/vouch/jsonl_server.py` (`_h_<name>` +
   `HANDLERS["kb.<name>"]`)
3. **`METHODS` list** in `src/vouch/capabilities.py`
4. **CLI command** in `src/vouch/cli.py` (the human mirror)

Plus a test under `tests/test_<feature>.py`.

If the method *reads* the KB, consider whether it should attach the
`_meta.vouch_salience` sidebar (built in `src/vouch/salience.py`,
attached inline by `kb_context` — see `kb_context` in
`src/vouch/server.py`). It's added per-tool — there's no global
decorator. Don't confuse it with `_meta.vouch_hot_memory`, which is
written only by the OpenClaw context engine's `assemble()`
(`src/vouch/openclaw/context_engine.py`); `src/vouch/hot_memory.py` is
the in-process session registry that *feeds* salience, not a response
field. (`_meta.vouch_trust` is separate again — stamped on every
dict-shaped result by a global wrapper, not per-tool.)

## Release flow

`release.yml` cuts a tagged PyPI release via Trusted Publishing on every
`v*` tag push.

Pre-release checklist (also in `CONTRIBUTING.md`):

1. Bump `version = "X.Y.Z"` in `pyproject.toml`.
2. Move everything under `[Unreleased]` in `CHANGELOG.md` into a dated
   `[X.Y.Z]` section.
3. `make check` green.
4. PR titled `chore(release): prepare X.Y.Z`, merge to `main`.
5. `git tag vX.Y.Z && git push --tags` — the workflow does the rest.
6. After CI finishes, draft the GitHub release with the CHANGELOG section
   as the body.

## What's not in scope right now

Don't propose:

* A SaaS mode / hosted vouch (explicitly out of scope; vouch is
  local-first by design)
* Removing the review gate "for trusted agents" (the `trusted-agent`
  config flag exists; the gate stays)
* Replacing yaml with json/sqlite as the storage format (the diff-in-PRs
  property requires plaintext)
* A custom config DSL (yaml + pydantic is sufficient)

Roadmap items that ARE in scope live in [`ROADMAP.md`](./ROADMAP.md) and
[`proposed-features.md`](./proposed-features.md) (local scratch — not on
`main`).

## Privacy

Same rule as gbrain's: never bake real customer names, internal URLs, or
PII into public artifacts. Test fixtures should use generic placeholders
(`alice-example`, `acme-example`).

## Hooks installed in this repo

Pre-commit checks the conventional-commit format. If you trigger it via
shell substitution in a `git commit -m "$(cat <<EOF…`, write the message
to `/tmp/commit-msg.txt` and use `git commit -F`.

## When you're stuck

* `make help` for the make targets.
* `vouch capabilities` for the JSON method surface.
* `git log --oneline -20` for recent commit voice.
* [`SPEC.md`](./SPEC.md) for the protocol contract.
* [`docs/getting-started.md`](./docs/getting-started.md) for the
  agent-side flow.
* The PR template at `.github/pull_request_template.md` if it's present.

Don't escalate to the user before you've checked the spec.
