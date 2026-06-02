# Adopting vouch for a Gittensor repo

Gittensor (Bittensor subnet 74) rewards open-source contributions, and its
scoring rules, repo allow-list, anti-sybil measures, and emission-split
decisions evolve and get debated across PRs, Discord, and validator changes.
That rationale usually lives in people's heads and scattered threads — so when
a weight changes or a repo is de-listed, there's no durable, cited answer to
*"why did we decide this, and what did it replace?"*

vouch is a good fit for that gap: a small, **review-gated, cited** knowledge
base committed to the repo as the durable memory layer for maintainer and
validator decisions.

## vouch vs. Gittensory — different layers

These are complementary, not competing:

| Layer | Owner | Holds |
|---|---|---|
| Chain / scoring | Gittensor (SN74) | the actual weights and emissions |
| **Live signals** | **Gittensory** | scores, queues, collision/reviewability |
| **Durable decisions** | **vouch** | *why* a rule exists, what it superseded — cited & reviewed |

vouch deliberately stores **no** live signals. It is not a validator or miner
client; it doesn't read on-chain scores, verify PATs, or submit weights. It is
the institutional memory that sits alongside the live layer.

## 1. Install

```bash
pipx install vouch-kb        # the installed command is `vouch`
vouch --version
```

## 2. Seed a KB with the gittensor pack

From the root of the Gittensor repo:

```bash
vouch init --template gittensor
```

This creates `.vouch/` and seeds a cited, approved starter pack about SN74
scoring — **1 source, 1 entity, 7 claims** (merged-PR rewards, PAT
verification, scoring factors, sybil-resistance, repo allow-list policy,
issue-solving multiplier, and emission split):

```bash
vouch status
#   durable: 7 claims  •  1 sources  •  1 entities  •  …
vouch search "scoring"
#   claim/gittensor-merged-pr-base-reward   …primary OSS reward signal…
#   claim/gittensor-sybil-resistance        …GitHub verification + merged-PR…
vouch doctor
#   index present, citations resolve, sources verify → clean
```

Commit it so the whole team shares one memory:

```bash
git add .vouch && git commit -m "chore: add vouch decision-memory KB"
```

`.vouch/.gitignore` keeps `proposed/` (drafts) and `state.db` (the derived
index) out of history automatically.

> **The seeded claims are starter-grade.** They summarize the scoring model as
> understood when the template was authored. Before you rely on a specific
> rule or number, `vouch show <claim-id>` it and `vouch supersede` it with the
> real spec/PR citation (see §4) so the KB reflects the live rules.

## 3. Wire the MCP server for agents

Add `.mcp.json` at the repo root so any MCP host (Claude Code, Cursor, Codex)
can query the KB and get cited answers instead of guessing:

```json
{
  "mcpServers": {
    "vouch": { "command": "vouch", "args": ["serve"] }
  }
}
```

An agent working in the repo can now call `kb.search` / `kb.context` ("how does
scoring work today?") and `kb.propose_claim` to draft new knowledge (still
gated — see below).

## 4. Capture decisions as cited claims

The whole value is that every scoring/policy decision is **proposed, reviewed,
cited, and supersede-able**. When a change lands:

```bash
# 1) register the thing you're citing — the PR, a spec file, a thread export
vouch source add docs/validator-change-pr-200.md      # → a source id <SRC>

# 2) propose a claim that cites it
vouch propose-claim \
  --text "SN74 raised the maintainer issue-solving multiplier from 1.66 to 1.75." \
  --source <SRC> --type fact --confidence 0.9 --tag gittensor --tag scoring
#   → proposal id <PID>

# 3) a *different* maintainer approves (the proposer can't self-approve)
vouch pending
vouch approve <PID>
git add .vouch && git commit -m "kb: record maintainer-multiplier change (PR #200)"
```

If you try to approve your own proposal you'll get
`forbidden_self_approval` — that's the gate working. A maintainer with a
different identity must approve.

**When a rule changes, supersede — don't overwrite.** Propose and approve the
replacement claim (steps 1–3 above), then link the old one to it by id:

```bash
vouch supersede <OLD_CLAIM_ID> <NEW_CLAIM_ID>
```

The old claim is kept (marked superseded) so the history of what changed stays
intact and queryable.

Every write is in `.vouch/audit.log.jsonl` — `vouch audit` shows exactly who
proposed and who approved each change, so the history of *why* is queryable,
not lost.

## 5. A CONTRIBUTING note for the repo

Drop a short note into the Gittensor repo's `CONTRIBUTING.md` so the habit
sticks:

```markdown
### Recording scoring / policy decisions

When a change alters scoring, the repo allow-list, anti-sybil thresholds, or
emission split, record it in vouch as a cited claim:

1. `vouch source add` the PR or spec that drives it.
2. `vouch propose-claim --source <id> --type fact|decision` (or
   `vouch supersede` the claim it replaces).
3. A maintainer reviews with `vouch pending` / `vouch approve`.

Cite the PR. Don't bury the rationale in a thread.
```

## 6. Day-to-day

```bash
vouch context "how are merged PRs scored and what stops sybil mining"
#   → a ranked, cited pack ready to paste into an agent prompt
vouch search "emission" --semantic    # if installed with the [embeddings] extra
vouch lint                            # broken citations / stale claims
```

That's the loop: live signals come from Gittensory; the durable *why* lives in
vouch, one cited and reviewed claim at a time.
