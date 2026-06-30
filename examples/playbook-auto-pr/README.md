# Auto-PR: open N mergeable PRs against a repo

`vouch auto-pr` opens pull requests against a github repo: it sources the
repo's open issues, bootstraps a contribution skill from the repo's own merged
PRs, has claude and codex fix-and-cross-verify each item, and opens a PR only
when the repo's own test gate is green and the reviewing engine signs off.

This is a **playbook**, not a live demo. `auto-pr` spawns the claude and codex
CLIs and pushes branches / opens PRs against a real GitHub repo over the
network — so `run.sh` does **not** execute it. it prints the exact command and
the external-tool requirements, then makes one read-only `kb.capabilities` call
to prove the KB surface is unaffected.

## Run it:

```bash
VOUCH=/path/to/vouch bash run.sh
```

`run.sh` prints the command, prints the `requires gh + claude + codex; not
auto-run` notice, and exits 0 without spawning any engine or opening any PR.

## The flow

```
vouch auto-pr https://github.com/acme-example/widget \
    --workspace ./work --count 1 --dry-run
```

1. **source work** — open issues first (filter with `--issue-label`), then
   agent-discovered improvements.
2. **bootstrap skill** — if the repo ships no contribution guidance, learn one
   from the repo's merged PRs.
3. **fix + cross-verify** — claude fixes, codex reviews (or vice versa),
   revising up to `--max-revise` rounds until the verifier signs off.
4. **test gate** — the repo's own test suite must go green.
5. **open PR** — only then does `gh pr create` run.

`--dry-run` runs every stage except `git push` / `gh pr create`.

## The sibling-tool boundary

auto-pr is a **sibling tool** to the knowledge base — it never writes to the
vouch KB. it creates no proposals, no claims, and no audit entries. the review
gate it depends on is the upstream repo's PR review, not vouch's review gate.
nothing auto-pr does lands in your knowledge base.

To make that boundary visible, `run.sh` builds a throwaway KB and makes a lone
`kb.capabilities` call. the KB surface is identical whether or not auto-pr ever
runs, and no `auto-pr` method appears on the KB surface at all:

```
=== kb.capabilities — the KB surface is unaffected by auto-pr ===
kb methods advertised: 54
kb.capabilities present: True
any auto-pr write method on the KB surface: False
```

## Requirements

Requires the `gh`, `claude`, and `codex` CLIs plus network access. **Not
auto-run** in this example — auto-pr spawns external coding agents and opens
PRs against a real GitHub repo, which is unsafe to execute unattended in an
examples harness. run it yourself against a repo you own.

## Methods demonstrated

- `kb.capabilities` — proves the KB surface is unaffected by the sibling
  auto-pr tool (read-only; auto-pr writes nothing to the KB).
