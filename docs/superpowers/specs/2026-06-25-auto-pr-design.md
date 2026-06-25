# auto-pr — design

date: 2026-06-25
status: approved (brainstorming)
topic: generalized auto-PR creation feature for vouch

## problem

the user maintains a constellation of repo-specific PR-creation skills
(`gittensory-pr-creation`, `metagraphed-pr-creation`, `openclaw-pr`,
`taopedia-site-pr`, …). each one teaches an agent how to make *one* mergeable
contribution to *one* repo. there is no generalized entry point: "point me at
any github repo and open N mergeable PRs, learning the repo's contribution
norms first if i don't already know them."

this feature is that entry point.

## north-star fit

vouch's load-bearing invariant is the review gate on knowledge-base writes.
this feature **does not write to the KB** and **does not touch**
`storage.py` / `proposals.py` / `lifecycle.py` / the audit log. it is a sibling
*tool* that ships in the vouch package — the same lane as a PR-tooling CLI
would occupy — and is deliberately isolated from the KB layer. nothing here
weakens the review gate.

## inputs / output

inputs (1:1 with the request):

| input | cli surface | meaning |
|---|---|---|
| original github project url | positional `<repo-url>` | the upstream repo to contribute to |
| workspace directory | `--workspace <dir>` | where the clone/fork lives (created if absent) |
| pr count | `--count <N>` | how many PRs to attempt |
| claude effort level | `--claude-effort <low\|medium\|high\|max>` | reasoning/model tier for the claude engine |
| codex effort level | `--codex-effort <low\|medium\|high\|max>` | reasoning/model tier for the codex engine |

output: the URLs of the PRs that were actually opened (one per line; a JSON
array under `--json`). attempts that fail verification are reported as
*skipped* with a reason — they are never opened. partial success (M ≤ N PRs
that genuinely pass) is the intended behaviour, not an error.

## architecture & placement

```
src/vouch/auto_pr.py        # core orchestration; subprocess boundary injected → unit-testable
src/vouch/cli.py            # + `vouch auto-pr` command group
skills/auto-pr/SKILL.md     # agent entry point / narrative wrapper
tests/test_auto_pr.py       # unit tests with a fake runner (no network, no real claude/codex)
```

the entire subprocess boundary (`git`, `gh`, `claude`, `codex`) is funnelled
through one injectable `Runner`. the default runner shells out; tests inject a
`FakeRunner` that returns canned results keyed on argv. this is what makes the
stage logic testable without a network or the real CLIs.

**dedup is self-contained.** `pr_cache` / the `pr-precheck` skill are not on
`main` (they live on unmerged branches), so auto-pr does its own dedup with a
direct `gh pr list --search` query and uses `vouch pr-cache` only
*opportunistically* if that subcommand happens to be installed. this keeps the
PR mergeable onto `main` with no cross-branch dependency.

## cli contract

```
vouch auto-pr <repo-url> \
  --workspace <dir> --count <N> \
  --claude-effort <low|medium|high|max> \
  --codex-effort  <low|medium|high|max> \
  [--issue-label good-first-issue] \
  [--fork-owner <login>] \
  [--max-revise 2] \
  [--dry-run] [--json]
```

- `--issue-label` filters the open-issue source (repeatable).
- `--fork-owner` overrides fork detection (default: the authenticated `gh` user).
- `--max-revise` caps the fixer↔verifier revise rounds per item (default 2).
- `--dry-run` runs every stage except `git push` / `gh pr create`; prints the
  branch + title + body it *would* open.
- `--json` emits the structured result array instead of bare URLs.

## data model

```python
@dataclass(frozen=True)
class WorkItem:
    kind: str            # "issue" | "discovered"
    title: str
    body: str            # issue body, or the discovery rationale
    slug: str            # branch-safe slug derived from the title
    number: int | None   # issue number when kind == "issue"
    url: str | None       # issue url when kind == "issue"

@dataclass(frozen=True)
class ReviewVerdict:
    approved: bool
    notes: str           # change-requests when not approved; sign-off otherwise

@dataclass
class PRResult:
    item: WorkItem
    status: str          # "opened" | "skipped"
    fixer: str           # "claude" | "codex"
    verifier: str        # the other engine
    url: str | None      # PR url when status == "opened"
    reason: str | None   # skip reason / verification notes when skipped
    rounds: int          # how many fix/verify rounds it took
```

## the subprocess boundary

```python
@dataclass
class RunResult:
    code: int
    stdout: str
    stderr: str

class Runner(Protocol):
    def run(self, argv: list[str], *, cwd: str | None = None,
            stdin: str | None = None, timeout: int | None = None) -> RunResult: ...
```

`SubprocessRunner` is the production implementation. `FakeRunner` (in tests)
matches argv prefixes to canned `RunResult`s.

## engine adapter & effort mapping

one `Engine` wraps a CLI headlessly. it has exactly two operations:

```python
class Engine:
    name: str            # "claude" | "codex"
    effort: str          # low|medium|high|max
    runner: Runner
    def fix(self, *, cwd: str, prompt: str) -> None:    # edits files in cwd autonomously
    def review(self, *, cwd: str, diff: str, prompt: str) -> ReviewVerdict:  # read-only judgement
```

headless invocations:

- **claude (fix)**:
  `claude -p "<prompt>" --permission-mode acceptEdits --model <model> --output-format json`
- **claude (review)**: same but `--permission-mode plan` (read-only); the
  prompt asks for a strict `APPROVE` / `REQUEST_CHANGES: <notes>` first line so
  the verdict is parseable.
- **codex (fix)**:
  `codex exec "<prompt>" --full-auto --cd <cwd> -c model_reasoning_effort=<eff> [--model <model>]`
- **codex (review)**: `codex exec "<prompt>" --sandbox read-only --cd <cwd> -c model_reasoning_effort=<eff>`

effort → flags (module-level dicts, easy to tune):

| level | claude `--model` | claude thinking | codex `model_reasoning_effort` |
|---|---|---|---|
| low | claude-haiku-4-5 | off | low |
| medium | claude-sonnet-4-6 | normal | medium |
| high | claude-opus-4-8 | normal | high |
| max | claude-opus-4-8 | extended | high |

(codex caps reasoning effort at `high`; `max` maps to `high`.)

## pipeline

run-level orchestrator `run_auto_pr(...) -> list[PRResult]`:

0. **resolve workspace** — if `--workspace` is already a git clone of the repo,
   use it; else `gh repo fork --clone <repo-url> <dir>` (or a plain clone when
   the caller has push access). sync the default branch.
1. **detect-or-bootstrap contribution guidance** — scan the clone for
   `CONTRIBUTING.md`, `AGENTS.md`, `CLAUDE.md`, `.claude/skills/**/SKILL.md`,
   `.codex/`, `.github/PULL_REQUEST_TEMPLATE.md`. if any exist → load them as
   fixer/verifier context. **if none exist → fetch merged PRs
   (`gh pr list --state merged --limit K --json …` + a handful of full diffs and
   review threads), hand them to an engine, and have it synthesize a
   contribution `SKILL.md`.** write that synthesized skill into the clone's
   `.claude/skills/auto-pr-contributing/SKILL.md` (plus a `.codex/`-readable
   mirror) so it is reused on the next run and by the fixer this run. this is the
   "create the skill by checking merged PRs" requirement.
2. **source N work items** — open *unassigned* issues first
   (`gh issue list --state open --search "no:assignee" [--label …]`), newest or
   most-reacted first; if fewer than N survive dedup, let the engines discover
   genuine bugs/improvements (a bounded "find one real, small, mergeable
   improvement" prompt) to fill the remainder. every candidate is dedup-checked
   (`is_duplicate`) before it becomes a `WorkItem`.
3. **for each item `i`** (sequential; isolated branch `auto-pr/<slug>` off the
   default branch):
   - `fixer = engines[i % 2]` (alternating claude/codex), `verifier = the other`.
   - **fix**: fixer runs headless with repo + guidance + issue text → edits +
     one conventional commit on the branch.
   - **local gate**: `detect_gate(clone)` → run the repo's own test/build
     (`make check` / `pytest -q` / `npm test` / `cargo test` / `go test ./...`);
     undetectable ⇒ gate skipped with a logged warning.
   - **verify**: verifier reviews the branch diff — does it solve the issue? is
     it mergeable? does it follow the repo's conventions? → `ReviewVerdict`.
   - **revise loop**: if the gate fails *or* the verdict is not approved, feed
     the failure/notes back to the fixer and retry, up to `--max-revise` rounds.
   - **decision**: still failing after the cap ⇒ `PRResult(status="skipped",
     reason=…)`, move on (never open a failing PR). passing ⇒ push the branch to
     the fork and `gh pr create` with a conventional title + lowercase body that
     links/closes the issue ⇒ `PRResult(status="opened", url=…)`.
4. **emit** — return the `PRResult` list; the CLI prints opened URLs and reports
   skips on stderr (or the full array under `--json`).

## house rules baked in (from the existing PR skills)

- conventional-commit titles; lowercase prose bodies.
- **no `Co-Authored-By` / AI-attribution trailer** anywhere.
- dedup before opening (no Nth duplicate of a tried/rejected fix).
- run the repo's own CI-equivalent gate locally; a red gate blocks the PR.
- one logical change per PR; link/close the issue it addresses.
- verification failure is a hard block — M genuine PRs beat N shaky ones.

## error handling

- **workspace resolution fails** (fork/clone error) → abort the whole run with a
  clear message; nothing was opened.
- **no work items** found (no open issues, discovery yields nothing) → exit 0
  with "nothing to do" and an empty result.
- **a single item fails** (fix errors, gate red, verifier rejects past the cap,
  push/`gh` error) → that item becomes `skipped` with the reason; the run
  continues with the next item. one bad item never sinks the batch.
- **engine CLI missing** (`claude`/`codex` not on PATH) → fail fast at startup
  with an actionable message naming the missing binary.

## testing strategy

`tests/test_auto_pr.py`, all against a `FakeRunner` (no network, no real CLIs):

- effort → flags mapping for both engines (table-driven).
- `source_work_items`: issues-first, then discovery fills the remainder to N.
- dedup: a candidate matching a cached/closed PR is dropped.
- fixer/verifier **alternation** across items (`i % 2`).
- **revise loop**: reject → revise → approve opens; reject past the cap skips.
- **gate**: red gate blocks; undetectable gate logs and proceeds.
- guidance **bootstrap**: triggered only when no contribution files exist; the
  synthesized skill is written to the expected path.
- `--dry-run`: no `git push` / `gh pr create` argv ever reaches the runner.
- top-level `run_auto_pr` returns the right opened/skipped split on a mixed batch.

`make check` (pytest + `mypy src` + `ruff`) must stay green. everything in
`auto_pr.py` is fully typed for the strict mypy gate.

## decisions taken (defaults, vetoable)

- **sequential** PRs in v1, isolated branch per item. worktree-based parallelism
  is a deliberate follow-up, not v1.
- **verification failure ⇒ skip** the item (not open-as-draft).
- dedup is self-contained via `gh`; `vouch pr-cache` is used only if present.

## out of scope (v1)

- parallel PR generation via worktrees.
- a hosted/daemon mode.
- editing the KB or routing through the review gate (this is a sibling tool).
- auto-responding to review comments after the PR is open (that is `pr-maintain`'s job).
