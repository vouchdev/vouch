# Mining Gittensor (SN74) with vouch

Gittensor (Bittensor subnet 74) pays miners in TAO for landing **merged** pull
requests into whitelisted open-source repos. Validators verify GitHub account
ownership via a fine-grained PAT, then score merged contributions by code
quality, each repo's allocation (its emission share), and programming-language
factors. The whitelist — the "master repositories" and their shares — is
**dynamic**: it sees audits, additions, de-listings, and share changes as the
subnet matures.

So mining well is a research loop: point a coding agent at a rotating set of
target repos and keep landing mergeable PRs in the ones that pay the most.

## The problem vouch solves: re-investigation

Every target repo is a cold start. To land a PR your agent has to work out:

- the repo's architecture and where the change belongs,
- how it builds and tests, and what CI must be green,
- the maintainer's merge bar (tests? a changelog entry? a signed CLA? small
  focused diffs?),
- which issues are actually worth solving, and which past attempts already got
  rejected — and why.

Do that across a dozen repos and a dozen sessions and your agent re-derives all
of it from scratch every time. That re-investigation is time you are **not**
spending landing PRs — and merged PRs are the only thing that earns.

vouch is a review-gated knowledge base that lives in the repo. With its Claude
Code hooks installed, each session's findings are captured, you approve the
ones worth keeping, and the next session recalls them — so the investigation
into a repo happens **once**, then compounds.

> vouch is **not** a validator or miner client. It never reads on-chain scores,
> verifies PATs, or submits weights — that is the `gitt` client's job (below).
> vouch is the reviewed memory of what your agent already figured out.

## 1. Install

```bash
pipx install vouch-kb        # the installed command is `vouch`
vouch --version
```

## 2. Initialise the KB

From the root of a target repo:

```console
$ vouch init
Initialised KB at ~/mining/acme-httpkit/.vouch
Seeded starter claim: vouch-starter-reviewed-knowledge
Next steps:
  vouch status
  vouch search agent
  vouch serve
```

Commit it so the KB travels with the repo:

```bash
git add .vouch && git commit -m "chore: add vouch KB"
```

`.vouch/.gitignore` keeps drafts (`proposed/`, `captures/`) and the derived
index (`state.db`) out of history automatically. Past the example claim `init`
seeds, everything durable arrives the same way: captured or proposed, then
reviewed — that is the loop §4 walks through, with real output.

## 3. Wire capture + recall into Claude Code

This is what makes the loop automatic. Install the hooks:

```bash
vouch install-mcp claude-code
```

That writes `.mcp.json` (so the agent can `kb.search` / `kb.context` the KB for
cited answers) **and** `.claude/settings.json`, which registers:

- a `PostToolUse` hook (`vouch capture observe`) that harvests each tool call
  into a gitignored scratch buffer,
- a `SessionEnd` hook (`vouch capture finalize`) that rolls the buffer plus a
  `git diff` backstop into **one pending session-summary page** — mechanically,
  no LLM, never auto-approved,
- a `SessionStart` hook that runs `vouch recall` (injecting approved knowledge)
  and nudges any pending summaries.

## 4. Mine one repo — a real session, captured

A real run against a whitelisted Go repo, `acme-httpkit`. Every snippet below
is actual `vouch` output (paths shortened; trimmed where marked `…`).

**Session 1.** A Claude Code session maps the codebase, reads
`CONTRIBUTING.md`, and works issue #212 (a connection-pool leak): a test run
fails along the way, the fix and the regression test the merge bar demands
land, and the changelog gets its entry. The `PostToolUse` hook harvests each
of those tool calls as they happen; at session end the `SessionEnd` hook rolls
them into one pending page:

```console
$ vouch pending
• 20260702-065648-dd727615  [page]  by vouch-capture
    session summary: acme-httpkit (9f4c2d1e-7a53-4b08-b6d2-3c1e5a90f7ab)
```

The rollup is mechanical — files touched, a `git diff` backstop, the command
trail with the failure preserved — and it is pending, not durable (page body
unescaped here for readability):

```console
$ vouch show 20260702-065648-dd727615
id: 20260702-065648-dd727615
kind: page
proposed_by: vouch-capture
session_id: 9f4c2d1e-7a53-4b08-b6d2-3c1e5a90f7ab
…
  ## git changes
  CHANGELOG.md      | 1 +
  transport/pool.go | 3 +++
  2 files changed, 4 insertions(+)
…
  ## observations
  - Read CONTRIBUTING.md
  - Grep idle
  - Read pool.go
  - Edited pool.go
  - Command failed: go test ./transport/ -run TestPoolReuse -v
  - Edited pool_test.go
  - Ran: go test ./transport/...
  - Edited CHANGELOG.md
…
status: pending
…
```

(The regression test is a new, untracked file, so it appears in the
observations but not in the `git diff` backstop.)

Approve the summary, then file the durable fact this run established — the
merge bar — citing the file it came from:

```console
$ vouch approve 20260702-065648-dd727615 --reason "accurate session summary"
Approved → page/session-summary-acme-httpkit-9f4c2d1e-7a53-4b08-b6d2-3c1e5a9

$ vouch source add CONTRIBUTING.md --title "acme-httpkit CONTRIBUTING.md"
3d99feccda605376e83524e9d8fcf7f40e5beb0b7d6ef9c472ad001c52d8cad0

$ vouch propose-claim \
    --text "acme-httpkit merges require 'make test' green and a CHANGELOG entry; PRs without a regression test are rejected." \
    --source 3d99feccda605376e83524e9d8fcf7f40e5beb0b7d6ef9c472ad001c52d8cad0 \
    --type fact --tag gittensor --tag merge-bar
20260702-065718-607dc50c
```

You proposed that claim, so you cannot also approve it:

```console
$ vouch approve 20260702-065718-607dc50c
✗ 20260702-065718-607dc50c: forbidden_self_approval: alice-example cannot approve their own proposal (set review.approver_role: trusted-agent in config.yaml to opt out)
Error: refusing to approve: 1 of 1 not approvable — nothing was approved (use --keep-going for best-effort)
```

A second reviewer — any identity but the proposer's — approves instead; vouch
takes the OS user (or `VOUCH_USER`) as the actor, so here the teammate reviews
from their own shell:

```console
$ VOUCH_USER=blake-example vouch approve 20260702-065718-607dc50c --reason "matches CONTRIBUTING.md"
Approved → claim/acme-httpkit-merges-require-make-test-green-and-a-changelog-
```

**Session 2.** The next session opens with the `SessionStart` hook running
`vouch recall`, which injects everything approved so far — the summary and
merge-bar claim the first session earned, plus the starter claim and page
`init` seeded (the page is elided below):

```console
$ vouch recall
<vouch-approved-knowledge>
# approved KB knowledge for this repo — 2 claim(s), 2 page(s). reviewed, cited, durable. …

## claims
- [acme-httpkit-merges-require-make-test-green-and-a-changelog-] acme-httpkit merges require 'make test' green and a CHANGELOG entry; PRs without a regression test are rejected.
- [vouch-starter-reviewed-knowledge] Vouch stores reviewed, cited knowledge in the repository so future agent sessions can retrieve agreed project context.

## pages
…
- [session-summary-acme-httpkit-9f4c2d1e-7a53-4b08-b6d2-3c1e5a9] session summary: acme-httpkit (9f4c2d1e-7a53-4b08-b6d2-3c1e5a90f7ab)
</vouch-approved-knowledge>
```

The agent already knows the layout, the merge bar (`make test` + a changelog
entry), and what #212's fix looked like — including the failing test run
captured along the way. It skips re-discovery and takes the PR to merge.

That is the whole point: the investigation into httpkit happened **once**.

## 5. Keep what's worth keeping — and supersede when it shifts

**The gate holds.** Captured summaries and proposed claims are `PENDING` until a
human approves them, and you cannot approve your own proposal
(`forbidden_self_approval`). That is a feature: it stops your agent from writing
its own history, so recalled memory is memory you vouched for.

**When the whitelist shuffles, supersede — don't overwrite.** Say you've filed
a targeting claim — httpkit is a high-value Go target — and its allocation
drops. Propose and approve the replacement claim, then link the old one to it:

```bash
vouch supersede <OLD_CLAIM_ID> <NEW_CLAIM_ID>
```

The old claim is kept (marked superseded) so the history of *what changed* stays
queryable — and your agent re-prioritizes toward a higher-allocation repo next
session instead of over-investing in a de-valued one.

## Where the live layer stops and vouch begins

The `gitt` client and the chain own everything live; vouch owns the durable
*why*. They don't overlap:

| Concern | Owner | Example |
|---|---|---|
| Register + broadcast your PAT | `gitt` miner client | `gitt miner post --wallet <name> --hotkey <hotkey>` |
| Check your miner status / scores | `gitt` miner client | `gitt miner check --wallet <name> --hotkey <hotkey>` |
| On-chain scoring + emissions | Gittensor (SN74) | validators score merged PRs |
| **What your agent learned about a repo** | **vouch** | merge bar, rejected approaches, targeting notes — cited & reviewed |

A typical miner setup runs both, side by side:

```bash
# live: broadcast ownership so merged PRs get attributed and scored
git clone https://github.com/entrius/gittensor.git && cd gittensor && uv sync
export GITTENSOR_MINER_PAT=ghp_your_token_here
gitt miner post  --wallet <name> --hotkey <hotkey>
gitt miner check --wallet <name> --hotkey <hotkey>

# memory: in each target repo, the vouch loop from §2–§4
```

## Day-to-day

```bash
vouch recall                          # what the next session should already know
vouch context "how do i land a mergeable PR in acme-httpkit"
#   → a ranked, cited pack ready to paste into an agent prompt
vouch search "merge bar" --limit 5
vouch lint                            # broken citations / stale claims
```

That's the loop: the `gitt` client and the chain handle the live signals; vouch
keeps the reviewed record of what your agent worked out, so it never works it
out twice — and every session you approve makes the next PR faster to land.
