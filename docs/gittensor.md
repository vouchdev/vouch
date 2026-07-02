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

## 2. Seed the scoring baseline

From the root of a target repo, seed a cited, approved starter pack describing
how SN74 scoring works today — so your agent knows what earns before it writes
a line:

```bash
vouch init --template gittensor
```

This creates `.vouch/` and seeds **1 source, 1 entity, 7 claims** (merged-PR
rewards, PAT verification, the scoring factors, sybil-resistance, the repo
allow-list policy, the issue-solving multiplier, and the emission split):

```bash
vouch status
#   durable: 7 claims  •  1 source  •  1 entity  •  …
vouch search "scoring factors"
#   claim/gittensor-scoring-factors   …code quality, repository allocation, language…
```

Commit it so the baseline travels with the repo:

```bash
git add .vouch && git commit -m "chore: add vouch KB with gittensor baseline"
```

> **The seeded claims are starter-grade** — they summarize the model as
> understood when the template was authored. `vouch show <claim-id>` one and
> `vouch supersede` it with the real spec/PR once you confirm the live rule
> (see §5).

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

## 4. Mine one repo — the loop

A concrete run against a whitelisted Go repo, `acme-httpkit`:

**Session 1 (Monday).** The agent maps the codebase, reads `CONTRIBUTING.md`,
runs the tests, and attempts issue #212 (a connection-pool leak). Its first PR
is rejected for lacking a regression test. Everything it did is auto-captured.
At session end you have a pending summary; approve it, and file the two facts
worth citing:

```bash
vouch pending
vouch approve <summary-id> --reason "accurate session summary"

# the durable, cited facts this run established:
vouch source add https://github.com/acme/httpkit/blob/main/CONTRIBUTING.md
vouch propose-claim \
  --text "acme-httpkit merges require 'make test' green and a CHANGELOG entry; PRs without a regression test are rejected." \
  --source <SRC> --type fact --tag gittensor --tag merge-bar
vouch propose-claim \
  --text "acme-httpkit carries a healthy SN74 allocation and is weighted toward Go — high value per merged PR." \
  --source <SRC> --type observation --tag gittensor --tag targeting

vouch pending            # a teammate (not you) approves — the gate holds
```

**Session 2 (Wednesday).** It opens with `vouch recall`, so the agent already
knows the layout, the merge bar (`make test` + a changelog entry), that #212's
first attempt was rejected for a missing regression test, and that httpkit is a
high-value Go target. It skips re-discovery, adds the regression test, and lands
the merged PR.

That is the whole point: the investigation into httpkit happened **once**.

## 5. Keep what's worth keeping — and supersede when it shifts

**The gate holds.** Captured summaries and proposed claims are `PENDING` until a
human approves them, and you cannot approve your own proposal
(`forbidden_self_approval`). That is a feature: it stops your agent from writing
its own history, so recalled memory is memory you vouched for.

**When the whitelist shuffles, supersede — don't overwrite.** If httpkit's
allocation drops, propose and approve the replacement claim, then link the old
one to it:

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
