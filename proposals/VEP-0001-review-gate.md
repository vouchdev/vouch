---
vep: 0001
title: Review gate
author: alaingm
status: final
created: 2026-05-01
landed-in: 0.0.1
---

# VEP-0001: Review gate

## Summary

Agents may *propose* writes to the knowledge base. They may not commit
them. A separate principal — usually a human running `vouch approve`
— must explicitly accept each proposal before any durable artifact is
written. Rejected and pending proposals never enter git history.

This is the feature that defines vouch's identity. Every other design
decision is downstream of it.

## Motivation

Two things are simultaneously true about LLM agents writing into a
knowledge base:

1. **They cannot be trusted with unmediated writes.** They hallucinate
   facts, misattribute sources, get prompt-injected, and don't notice
   when they're wrong. A KB that accepts whatever the agent says is
   not a KB — it's a transcript of agent confusion.
2. **Asking a human before every fact is too slow** to be a real
   product. If the human is reviewing every line in the moment, the
   agent isn't accelerating anything.

The compromise that other tools have not made: let the agent write
*proposals* immediately, and let the human review them in batch later,
the same way they review pull requests. Decouple the timing of
*write* from the timing of *commit*.

Existing options sit at the extremes. mem0/Letta accept whatever the
agent writes. LLM-Wiki tools accept whatever the agent writes. There
is a gap in the market for a KB that takes review seriously.

## Proposal

Add two file-layout concepts:

- `.vouch/proposed/<id>.yaml` — pending proposals. Gitignored.
- `.vouch/decided/<id>.yaml` — accepted or rejected proposals. Committed.

Add four `kb.*` methods:

- `kb.propose_claim`, `kb.propose_page`, `kb.propose_entity`,
  `kb.propose_relation` — agent-facing. Write to `proposed/`.
- `kb.approve` — host-trusted. Validates the proposal, writes the
  durable artifact, moves the proposal to `decided/` with
  `status: approved`.
- `kb.reject` — host-trusted. Moves the proposal to `decided/` with
  `status: rejected` and a required reason.

Add a `Proposal` model: kind, payload, proposed_by, rationale, status,
decided_at, decided_by, decision_reason.

Block self-approval by default: if `VOUCH_AGENT` at approve time
equals `proposed_by`, the call is rejected unless
`review.approver_role: trusted-agent` is set in config.

## Design

State machine and crash semantics: see [spec/review-gate.md](../spec/review-gate.md).

Atomic-ish write order on approve:

1. Re-validate the payload (sources may have changed).
2. Write durable artifact.
3. Move proposal to `decided/`.
4. Emit audit events.

If step 2 succeeds but step 3 fails, `vouch doctor` surfaces the
inconsistency and `vouch approve <id>` is re-runnable (it's idempotent
on the durable artifact).

## Compatibility

This is the founding VEP. There is nothing to be compatible with.

For consumers of *future* vouch versions: the review-gate state machine
is part of the 1.0 freeze. Removing it would be a fork, not a release.

## Security implications

The review gate is the *primary* security boundary. Its assumptions:

- The approver is not the same principal as the proposer (default).
- The audit log captures the actor on every state transition.
- `proposed/` is gitignored, so a malicious agent cannot smuggle data
  into the project's git history by filing proposals.

What the gate does *not* defend against, by design:

- A compromised approver. If the human running `vouch approve` is
  social-engineered, the gate provides no protection beyond the audit
  trail.
- Poisoned source content. Hashes pin bytes; they don't validate
  semantics.

## Performance implications

`kb.propose_*` writes one small YAML file and emits one audit event.
On commodity hardware this is sub-millisecond. The hot path is
unaffected.

`kb.approve` does a re-validation pass and one file move. Approving a
large batch with `--batch` (post-0.1) will amortise the validation
load via a single transaction.

## Open questions

Resolved by 0.0.1:
- ~~Should rejected proposals be deletable?~~ No. They're part of the
  audit record.
- ~~Should there be a "needs more info" state?~~ No, for now. Reviewers
  can reject with reason; agents re-propose.

Deferred:
- Should approval support multi-sig (require N approvers)? Probably
  a 0.2 VEP.
- Should there be a "trust score" decay that auto-rejects proposals
  from an agent whose recent approvals are mostly rejected? Probably
  not — this leaks into adversarial territory and is fragile.

## Alternatives considered

**Just write directly, log everything.** This is what mem0/Letta do.
You get speed; you lose the ability to keep agent hallucinations out
of canonical state. Inadequate for any KB that informs real decisions.

**Per-write LLM-as-judge.** A second LLM reviews each agent write. We
considered this and rejected it: it inherits all the failure modes of
LLMs and creates a recursion problem (who reviews the reviewer?). The
human-in-the-loop is the point.

**Branch-per-proposal with PRs.** Each proposal opens a git branch and
a PR. Considered; would couple vouch tightly to a specific git host.
The file-based gate works against any VCS or none.

## References

- [spec/review-gate.md](../spec/review-gate.md) — full state machine.
- [SECURITY.md](../SECURITY.md) — threat model.
