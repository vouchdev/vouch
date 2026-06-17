# Multi-agent setups

Running more than one agent against the same `.vouch/` directory.

## Identity

The single most important step: **give every agent its own
`VOUCH_AGENT`**.

```bash
# In Claude Code's .mcp.json:
"env": {"VOUCH_AGENT": "claude-code-anna"}

# In Cursor's mcp.json:
"env": {"VOUCH_AGENT": "cursor-anna"}

# In the codex CLI's config.toml:
[mcp_servers.vouch.env]
VOUCH_AGENT = "codex-anna"
```

The audit log records the value as `actor`. Proposals carry it as
`proposed_by`. Without distinct identities, your audit trail is
useless: "someone proposed something".

A common convention: `<host>-<human>` so you can tell apart "Alice
running Claude Code" from "Bob running Claude Code" from "Alice
running Cursor".

## Concurrency

vouch is single-writer per file. Two agents proposing simultaneously
each get their own proposal id; no lock contention. Two agents
approving simultaneously can race — the second one through sees
`already_decided`.

State.db (the FTS5 index) is updated under SQLite's normal locking.
Reads do not block reads; writes briefly block other writes.

Bottom line: don't worry about concurrent agents writing. *Do* worry
about concurrent approvals if you have a script approving in bulk —
serialise them.

## Approval policies

A few patterns:

**Single-approver.** One human approves everything. Default. Works
fine up to ~5 agents.

**Per-agent approver.** Different humans approve different agents'
output. Encode in policy (people-process), not in vouch — there's no
built-in routing.

**Trusted-agent peer approval.** Agent A approves Agent B's proposals
and vice versa. Set `review.approver_role: trusted-agent` in
`config.yaml`. **Read [SECURITY.md](../SECURITY.md) first** — this
gives up the human-in-the-loop guarantee in exchange for autonomy.

**N-of-M.** Not supported today. On the roadmap (post-0.1, requires a
[VEP](../proposals/README.md)).

## Conflict patterns

Two agents will eventually disagree. What it looks like:

- **Duplicate proposals.** Two agents independently propose the same
  fact. The reviewer sees two pending proposals with similar text.
  `kb.propose_claim` / `vouch propose-claim` return non-blocking
  `warnings` (`similar_approved`, `similar_pending`) when embeddings are
  available — approve one, reject the other with reason "duplicate of prop-XYZ".
- **Contradicting claims approved.** Two reviewers approved
  conflicting claims at different times. Use `vouch contradict A B` to
  link them; pick a survivor with `vouch supersede`.
- **Stale knowledge.** Agent A's claim from January says "we use
  Redis"; Agent B's claim from April says "we use Memcached". Both
  approved. Run `vouch lint --stale-days 60` to surface old claims.

## Tracking who's busy

```bash
vouch stats              # pending by agent, review rates, citation coverage
vouch stats --json       # same, for dashboards / CI
```

Or, if you only need the queue breakdown:

```bash
vouch pending --json | jq -r '.[] | "\(.proposed_by)\t\(.id)"' | sort | uniq -c
```

Useful when one agent has been spammy or is asleep at the wheel.

## Crystallisation per agent

When an agent ends a session, `kb.crystallize` produces a
session-summary page. With multiple agents, each gets its own session
page. Tag them with the agent name so they're filterable:

```bash
vouch session start --task "implement password reset" \
                    --note "tag:agent:claude-code-anna"
```

## Distributed sync

When two teammates each have their own `.vouch/` directory, use the
sync workflow to reconcile them deterministically:

```bash
vouch sync-check ../other-repo
vouch sync-apply ../other-repo --on-conflict fail
```

`sync-check` accepts either another repo / `.vouch` directory or a
bundle. It reports new files, identical files, and conflicts without
writing anything. `sync-apply` imports non-conflicting files only; it
never overwrites reviewed knowledge. Use `--on-conflict skip` to leave
conflicts untouched, or `--on-conflict propose` to write a local conflict
report under `proposed/sync-reports/` for human review. `config.yaml`
stays local to each KB and is not synced.

## What doesn't work yet

- **Live merge conflicts.** Two agents editing the same proposal at
  once isn't a scenario vouch addresses — agents create proposals,
  they don't edit existing ones.

## Tips

- Periodically run `vouch audit --tail 50` and skim. Patterns
  (which agents propose what, how often they're rejected) surface
  fast.
- Build a habit of `vouch lint` weekly. Stale-claim accumulation is
  the silent killer in multi-agent setups.
- If an agent's rejection rate is high, look at *why* — usually
  prompt drift, not the agent being "bad". The audit log carries
  `decision_reason` for exactly this.
