# Governance

vouch is pre-1.0 and small. This document describes how decisions get made
*today*; expect it to evolve as the project grows.

## Roles

**Maintainers** have commit access on `main` and merge rights. Today there
is one maintainer. Adding a maintainer requires unanimous agreement of
existing maintainers.

**Contributors** are anyone who opens an issue or PR. No formal status.

**Approvers** is a vouch-internal term meaning whoever is allowed to run
`vouch approve` on a given `.vouch/` deployment — that's an operational
concept inside the tool, not a project role.

## Decisions

Three kinds of decisions, three different bars:

1. **Day-to-day code changes** — bug fixes, refactors, doc tweaks,
   new tests. A single maintainer review suffices. PRs from
   non-maintainers need a maintainer reviewer.

2. **Surface changes** — anything touching the object model, the `kb.*`
   method surface, the on-disk layout in `.vouch/`, the bundle format,
   or the audit-log shape. These require a **[VEP](proposals/README.md)**
   (Vouch Enhancement Proposal): a written design doc filed under
   `proposals/`, discussed in public, and explicitly accepted by a
   maintainer before implementation. If two or more maintainers exist,
   acceptance requires majority.

3. **Project-level changes** — changing this document, changing the
   license, electing/removing maintainers, changing the project
   direction. Requires consensus among all maintainers and a 14-day
   public comment window on a PR or issue.

## Contribution areas — who may change what

The three decision bars above map onto concrete parts of the tree. This is the
practical version for a contributor deciding where to start, and it is enforced
by [`.github/CODEOWNERS`](.github/CODEOWNERS) and
[`.github/workflows/auto-merge.yml`](.github/workflows/auto-merge.yml).

### Core (owner-only)

The load-bearing invariants — the "surface changes" bar above, made concrete. A
mistake here can make verification forgeable, let a write skip the gate, corrupt
the authoritative history, or break a contract downstream tools depend on. Only
a code owner merges these:

| Area | Paths | Why |
|---|---|---|
| Review gate + lifecycle | `src/vouch/proposals.py`, `lifecycle.py` | every write must go through `approve()` |
| Verification | `src/vouch/receipts.py` | the byte-offset receipt is the differentiator |
| Authoritative history | `src/vouch/audit.py` | the audit log is append-only |
| Storage + index | `src/vouch/storage.py`, `index_db.py` | a torn write or lost fsync is data loss |
| Object model / schema | `src/vouch/models.py`, `migrations/` | the on-disk + bundle contract |
| Method surface + transports | `capabilities.py`, `server.py`, `jsonl_server.py` | MCP/JSONL/CLI parity |
| Protocol | `SPEC.md`, `spec/` | what downstream tools code against |
| Packaging + version | `pyproject.toml`, `openclaw.plugin.json`, `package.json`, `src/vouch/__init__.py` | the four-site version invariant |
| Supply chain | `.github/workflows/`, `.github/dependabot.yml`, `Dockerfile`, `install.sh` | a workflow edit can exfiltrate secrets |

Adding a new `kb.*` method spans four of these files, so it is core too — file a
VEP first.

### Contributor-friendly (any maintainer reviews)

Where most contributions land: behavior, not invariants. Any maintainer merges
on green CI. Host adapters (`adapters/<host>/`, `install_adapter.py`), retrieval
and capture quality (`extract.py`, `recall.py`, `context.py`, `salience.py`,
`compile.py`, `sessions.py`, `hooks.py`, `fetch.py`, `notify.py`, `stats.py`,
`metrics.py`, thin `cli.py` commands), the web console (`webapp/`,
`src/vouch/web/`), and tests (`tests/**`).

### Auto-merge safe (green CI merges it, no human)

Prose that cannot change how the code runs — `docs/**`, top-level markdown
(`README.md`, `CHANGELOG.md`, …), and `.github/ISSUE_TEMPLATE/**`. Excluded even
though they are text: `SECURITY.md`, this file, `CONTRIBUTING.md`, `CODEOWNERS`,
and `spec/**` (the protocol contract). `examples/**` is *not* auto-merged — it
can carry runnable code, so a maintainer reads it (Tier 1).

## How PRs merge

Two mechanisms live in `.github/`:

- **`CODEOWNERS`** forces a code-owner review on any core path — the hard
  backstop — once branch protection's "Require review from Code Owners" is on.
- **`auto-merge.yml`** enables GitHub auto-merge on a prose-only PR so it merges
  itself on green CI. It reads only the changed-file list (it never runs PR
  code), and enabling auto-merge still waits for the required checks.

**Owner setup (one-time, repo Settings, on `test` and `main`):** enable *Allow
auto-merge*; add a branch-protection rule that *requires a PR*, *requires review
from Code Owners*, and *requires the `ci` status checks*; set the owner handle in
`CODEOWNERS`. Without required checks, auto-merge has nothing to wait on and
merges nothing.

This is vouch applied to vouch: a docs PR is a claim whose receipt verifies —
mechanically safe, auto-approved; a change to `proposals.py` touches the gate
itself, and no receipt can vouch for that, so a person decides.

## Disagreements

If contributors disagree on a PR, the assigned maintainer makes the call.
If contributors disagree with a maintainer, escalate by opening an issue
labelled `governance:` and naming the dispute. Maintainers respond
publicly within two weeks.

There is no separate "BDFL" or steering committee at this scale. If the
project grows past one maintainer, this document will be revised to add
quorum rules and a written tiebreaker.

## Code of Conduct

All project spaces are governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Conduct enforcement is a maintainer decision, not a community vote.

## License & contribution terms

vouch is MIT-licensed. Contributions are assumed to be offered under the
same terms — there is no CLA. If your employer requires a CLA, raise it on
your PR and we'll work it out.

## Forking

The license permits forks. We'd appreciate a heads-up issue if you fork
with the intent to publish under a similar name, so users don't get
confused — that's a courtesy, not a rule.
