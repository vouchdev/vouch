# Review gate — state machine

The review gate is vouch's defining feature: agents never write durable
artifacts directly. Every write is a *proposal* that a human (or
trusted approver) must explicitly accept.

This document specifies the state machine, the file moves, and the
audit events.

---

## States and transitions

```
                       kb.propose_*
                  ┌──────────────────────┐
                  │                      ▼
              ┌───┴────┐         ┌─────────────┐
   (nothing)──┤ create ├────────►│  PENDING    │
              └────────┘         │  proposed/  │
                                 └──────┬──────┘
                                        │
                       ┌────────────────┼────────────────┐
                       │                │                │
                kb.approve         kb.reject         (timeout)
                       │                │                │
                       ▼                ▼                ▼
              ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
              │  APPROVED   │   │  REJECTED   │   │  STALE      │
              │ + durable   │   │  decided/   │   │ (gc target) │
              │   artifact  │   │             │   │             │
              └─────────────┘   └─────────────┘   └─────────────┘
```

### PENDING

- File location: `.vouch/proposed/<id>.yaml`
- Gitignored: yes
- The proposal's `payload` carries the unbuilt object (claim/page/entity/relation).
- The proposal MUST validate at creation time. If it doesn't, no file
  is written and the call returns `valid: false` with errors.

### APPROVED

`kb.approve <id>` does *all* of the following, atomically (best-effort,
with rollback on failure):

1. Validates the payload again (claims may have become invalid if their
   cited sources were removed since proposal).
2. Writes the durable artifact to its kind-specific directory:
   - claim → `.vouch/claims/<id>.yaml`
   - page → `.vouch/pages/<id>.md` (frontmatter + body)
   - entity → `.vouch/entities/<id>.yaml`
   - relation → `.vouch/relations/<id>.yaml`
3. Stamps `approved_by` on the artifact (claims only — entities/relations don't carry an approver field).
4. Moves the proposal from `proposed/` to `decided/` with
   `status: approved`, `decided_at`, `decided_by`, `decision_reason`.
5. Emits `proposal.approve` + the kind-specific create event
   (`claim.create`, `page.create`, etc.) to `audit.log.jsonl`.
6. Updates `state.db` index entries for the new artifact.

### REJECTED

`kb.reject <id> --reason "..."` does:

1. Moves the proposal from `proposed/` to `decided/` with
   `status: rejected`, `decided_at`, `decided_by`, `decision_reason`.
2. Emits `proposal.reject` to `audit.log.jsonl`.
3. **Does not write a durable artifact.**

The `--reason` MUST be non-empty. Rejections without reasons are bad
review hygiene.

### STALE (informative)

Implementations MAY garbage-collect `proposed/<id>.yaml` files older
than a configurable threshold. The default threshold is 90 days. GC
emits `proposal.expire` and moves the file to `decided/` with
`status: rejected`, `decision_reason: "expired"`.

`decided/` is never GCed — it's the audit record.

---

## Idempotency

- `kb.propose_*` is **not** idempotent. Two identical proposals get two
  distinct ids. (Agents do retry, but the reviewer should see retry
  noise.)
- `kb.approve` and `kb.reject` are idempotent on a terminal proposal —
  re-calling with the same id returns the existing decision rather
  than erroring. They MUST error if the proposal is in the *other*
  terminal state (you can't approve a rejected proposal).

---

## Authorization

The `approver_role` setting in `config.yaml` controls who may run
`kb.approve` / `kb.reject`:

- `human` (default) — `decided_by` MUST be a non-agent actor. The
  server inspects `VOUCH_AGENT`; if set and matches `proposed_by`, the
  call is rejected with `forbidden_self_approval`.
- `trusted-agent` — any actor may approve, including the proposing
  agent. This is for fully autonomous setups; the audit log still
  records the actor.

Implementations MAY add other roles. The spec only mandates the two
above.

---

## Lifecycle methods are not gated

The lifecycle methods (`kb.supersede`, `kb.contradict`, `kb.archive`,
`kb.confirm`, `kb.cite`) mutate **already-reviewed** artifacts. They
don't introduce new assertions — they're metadata about how reviewed
assertions relate. Gating them would be a security-theatre tax: the
agent could just propose a contradicting claim instead.

So lifecycle methods are direct + audited. The audit event names are:

- `claim.supersede`, `claim.contradict`, `claim.archive`,
  `claim.confirm`, `claim.cite`.

These appear in `audit.log.jsonl` exactly like create events.

---

## Crash semantics

If a process crashes mid-`approve`:

- If the durable artifact was written but the proposal not yet moved
  to `decided/`: re-running `kb.approve` MUST detect the existing
  artifact, complete the move to `decided/`, and not duplicate the
  durable write.
- If the proposal was moved to `decided/` but the artifact write
  failed: the inconsistency is detectable by `vouch doctor`, which
  surfaces "decided/X.yaml says approved but claims/X.yaml is missing".
  Recovery is re-running `vouch approve <id> --force-rewrite` (CLI
  detail; not part of the `kb.*` surface).

Implementations SHOULD write the durable artifact first, then move the
proposal. That ordering keeps `decided/` honest: if it says
`approved`, the artifact exists.
