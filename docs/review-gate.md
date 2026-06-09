# The review gate — for users

The review gate is what makes vouch *vouch*. This doc explains it from
the operator's seat — what you do, what the agent does, and what
happens on disk.

For the normative state machine, see [../spec/review-gate.md](../spec/review-gate.md).
For *why* we built it this way, see [VEP-0001](../proposals/VEP-0001-review-gate.md).

## Mental model

```
   AGENT                          YOU                           DISK
   ─────                          ───                           ────
                                                            (empty)

   kb_propose_claim ──────────►   "you have 1 pending"
                                  vouch pending
                                  vouch show prop-…
                                                            .vouch/proposed/prop-….yaml
                                                                  ↑ gitignored
                                  vouch approve prop-…
                                          │
                                          ▼
                                                            .vouch/claims/auth-uses-jwt.yaml
                                                            .vouch/decided/prop-….yaml
                                                                  ↑ committed
```

The agent writes immediately (fast). You decide later (in bulk, on
your schedule, in a PR-shaped review). The durable artifact appears
only after you decide.

## The everyday flow

### As an operator

```bash
vouch status               # snapshot
vouch pending              # what's waiting
vouch show <id>            # full proposal details
vouch approve <id>         # → durable artifact + decided/
vouch approve <id> <id>    # approve several reviewed proposals at once
vouch reject  <id> --reason "duplicates auth-uses-jwt"
```

Then commit:

```bash
git add .vouch && git commit -m "kb: review batch"
```

Cadence matters. Approving daily keeps proposals fresh; approving
weekly is fine; approving never is the failure mode — your KB just
collects noise the agent can't see (since `proposed/` is local).

### As an agent

The agent's tools are `kb_propose_*` for writes, `kb_search` /
`kb_context` / `kb_read_*` for reads, and the lifecycle helpers
(`kb_supersede`, `kb_contradict`, `kb_archive`, `kb_confirm`,
`kb_cite`) for adjustments to already-approved knowledge.

Agents don't have an `approve` tool. They cannot self-promote.

## Self-approval (and how to *let* it)

By default, a proposal cannot be approved by the same actor that filed
it. The server compares `VOUCH_AGENT` (the env var the host process
sets) against the proposal's `proposed_by`; matching identities trip
the `forbidden_self_approval` error.

In some setups — a fully autonomous agent running in a sandbox — you
*want* self-approval. Flip it in `.vouch/config.yaml`:

```yaml
review:
  approver_role: trusted-agent
```

The audit log still records the actor. You're trading the gate for
auditability-only.

## When approvals are wrong

You will approve something you shouldn't. The remedies:

- **Archive** the bad claim (`vouch archive <claim-id>`). It stays in
  the file system and audit log but `status: archived`.
- **Supersede** with a corrected version (`vouch supersede <old> <new>`).
  Best when the *idea* was right and the *details* were wrong.
- **Redact**, for the rare case where the content itself is harmful
  (PII, a leaked secret). Redaction zeros the body and is *not*
  reversible.

You cannot un-approve. The decision is part of the record. This is on
purpose.

## When rejections are wrong

If you reject a proposal that turned out to be correct, the agent (or
you) can re-propose. The new proposal gets a new id. The old rejected
proposal stays in `decided/` — the history shows you rejected once,
then accepted later. That's honest.

## Lifecycle methods bypass the gate (and that's fine)

These methods write directly to disk without going through `proposed/`:

- `kb.supersede` — links an old claim to a new one
- `kb.contradict` — marks two existing claims as conflicting
- `kb.archive` — flips status
- `kb.confirm` — bumps `last_confirmed_at`
- `kb.cite` — appends an evidence id to a claim

Why no gate? Because none of these *create new assertions*. They
metadata-tag already-reviewed knowledge. If the agent wants to assert
something new, it must propose. If it wants to mark something it just
read as "still true", `kb.confirm` is fine — the underlying claim was
already approved.

This narrow exception keeps the everyday agent UX fluid. Agents
constantly need to mark stale claims as such; gating that would create
review overhead with no protection benefit.

## CI / bots

You *can* approve from CI:

```bash
echo '{"id":"r1","method":"kb.approve","params":{"proposal_id":"prop-…"}}' \
  | vouch serve --transport jsonl
```

Whether you *should* is a different question. Auto-approval re-creates
the problem the gate solves. If you do it:

- Restrict to a specific `proposed_by` (one bot, not all agents).
- Restrict to a specific kind (e.g. only `entity` proposals, never
  `claim`).
- Audit weekly.
- Be ready to revoke when the bot starts proposing nonsense.

See [adapters/jsonl-shell/](../adapters/jsonl-shell/) for an example
auto-approver.

## Common questions

**Q: I committed `proposed/` once by accident — does it matter?**
Mostly cosmetic. The proposals are decided one way or the other on
next review; you can remove the entries with `git rm` afterwards.
Update your local `.gitignore` so it stops happening.

**Q: Can I approve in bulk?**
Yes. Review proposals, then approve them together:

```bash
vouch approve <id> <id> --reason "reviewed together"
```

Default is all-or-nothing: if any id is invalid or already decided,
nothing is approved. Use `--keep-going` for best-effort approval.

**Q: What if I want a *softer* gate — "warn, don't block"?**
You don't. If you want soft, use a tool without a gate. vouch's
identity is the gate; making it optional makes vouch worse at the one
thing it does.
