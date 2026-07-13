# Build your first knowledge base

By the end of this tutorial you'll have a vouch knowledge base in a git repo,
one reviewed-and-approved claim backed by a real source, and the ability to
recall it three ways — ranked search, a task-shaped context pack, and a cited
synthesis. You'll also see the review gate refuse to let you approve your own
proposal, which is the single most important thing vouch does.

- **Time:** about 15 minutes
- **Cost:** none — no API keys, no network
- **You'll need:** Python 3.11+, git, and a terminal

The scenario: your project decided to use JWTs for auth, and you're tired of
your agent (and your teammates) re-litigating that decision every few weeks.
You want one reviewed, cited answer that any future session can read.

## 1. Install and verify

```bash
pipx install vouch-kb            # the command stays `vouch`
vouch --version
```

(From a clone instead: `python -m venv .venv && . .venv/bin/activate && pip
install -e '.[dev]'`.)

## 2. Initialise a KB

vouch lives inside a git repo. From your project root:

```bash
git init -q                      # skip if the repo already exists
vouch init
```

```
Initialised KB at /your/project/.vouch
Seeded starter claim: vouch-starter-reviewed-knowledge
Next steps:
  vouch status
  vouch search agent
  vouch serve
```

`vouch init` creates `.vouch/` with `config.yaml`, an append-only
`audit.log.jsonl`, the directory skeleton (claims, pages, entities, relations,
sources, sessions, proposed, decided), and a `.gitignore` that keeps
`proposed/` and the derived `state.db` out of version control. It also seeds
one starter claim so the KB isn't empty.

```bash
vouch status
```

```
KB at /your/project/.vouch
  durable: 1 claims  •  1 pages  •  1 sources  •  0 entities  •  0 relations
  pending: 0 proposals
  audit:   1 events  •  index: present
```

## 3. Register a source

Claims have to cite something. A source is any content-addressable
evidence — a file, a URL, a transcript. Register the decision record:

```bash
mkdir -p docs/decisions
cat > docs/decisions/auth.md <<'EOF'
# Auth decision meeting, April 2026

We agreed auth uses JWTs in the Authorization header.
EOF

vouch source add docs/decisions/auth.md --title "Auth decision meeting, April 2026"
```

```
532e2b4d95c9dd17aa1c0feed366746ca47e40e4cb8e8582f3677b0931b55d70
```

That long hex string is the source id — a sha256 of the content. Re-running
`source add` on the same bytes is a no-op; sources dedupe by content hash, so
you never end up with two copies of the same evidence.

## 4. Propose a claim

A claim is a single reviewable statement. Propose one, citing the source you
just registered:

```bash
vouch propose-claim \
  --text "Auth uses JWTs in the Authorization header." \
  --source 532e2b4d95c9dd17aa1c0feed366746ca47e40e4cb8e8582f3677b0931b55d70 \
  --type decision \
  --confidence 0.95
```

```
20260630-073841-d722ac24
```

That's the proposal id (a timestamp plus a short hash). The proposal lives in
`.vouch/proposed/` — local-only, gitignored, **not yet part of the KB**. Look
at the queue:

```bash
vouch pending
```

```
• 20260630-073841-d722ac24  [claim]  by a
    Auth uses JWTs in the Authorization header.
```

```bash
vouch show 20260630-073841-d722ac24
```

```
id: 20260630-073841-d722ac24
kind: claim
proposed_by: a
payload:
  id: auth-uses-jwts-in-the-authorization-header
  text: Auth uses JWTs in the Authorization header.
  type: decision
  confidence: 0.95
  evidence:
  - 532e2b4d95c9dd17aa1c0feed366746ca47e40e4cb8e8582f3677b0931b55d70
status: pending
```

## 5. Review it — the gate

This is the step that makes vouch vouch. Try to approve your own proposal:

```bash
vouch approve 20260630-073841-d722ac24 --reason "matches the meeting notes"
```

```
✗ 20260630-073841-d722ac24: forbidden_self_approval: a cannot approve their
  own proposal (set review.approver_role: trusted-agent in config.yaml to opt out)
Error: refusing to approve: 1 of 1 not approvable — nothing was approved
```

vouch refused. The actor who proposed a claim cannot be the actor who approves
it — that's the whole point of a review gate. Approval has to come from a
*different* identity. vouch resolves the actor from `VOUCH_AGENT` (falling back
to your system user), so a human reviewer approves like this:

```bash
VOUCH_AGENT=alice-reviewer vouch approve 20260630-073841-d722ac24 \
  --reason "matches the meeting notes"
```

```
Approved → claim/auth-uses-jwts-in-the-authorization-header
```

The claim is now durable: a plain YAML file at
`.vouch/claims/auth-uses-jwts-in-the-authorization-header.yaml`, and the
proposal record moves to `.vouch/decided/` for audit.

> Working solo and don't want two identities? Set `review.approver_role:
> trusted-agent` in `.vouch/config.yaml` to opt out of the self-approval check.
> The gate still records who approved what — you've just told it you trust the
> proposer to review themselves. For anything shared, leave it on.

## 6. Recall it — three ways

**Ranked search** — "does the KB know X?":

```bash
vouch search "JWT"
```

```
claim/auth-uses-jwts-in-the-authorization-header	Auth uses JWTs in the Authorization header.  (substring)
```

**A context pack** — a task-shaped working set, ready to drop into an agent
prompt, with citations and a quality gate:

```bash
vouch context "how does auth work"
```

```json
{
  "backend": "fts5",
  "items": [
    {
      "id": "auth-uses-jwts-in-the-authorization-header",
      "summary": "Auth uses JWTs in the Authorization header.",
      "citations": ["532e2b4d95c9dd17aa1c0feed366746ca47e40e4cb8e8582f3677b0931b55d70"]
    }
  ]
}
```

**A cited synthesis** — a direct answer built only from approved claims, with
inline citation markers:

```bash
vouch synthesize "how does auth work"
```

```json
{
  "answer": "Auth uses JWTs in the Authorization header [auth-uses-jwts-in-the-authorization-header].",
  "claims": ["auth-uses-jwts-in-the-authorization-header"]
}
```

`synthesize` never invents — if the KB doesn't have it, the answer says so.
Every sentence traces back to a claim that went through the gate.

## 7. Trace the provenance

Because every write is an event, you can always answer "why does this claim
exist, and who put it there?"

```bash
vouch why auth-uses-jwts-in-the-authorization-header
```

```
why auth-uses-jwts-in-the-authorization-header (claim)
  approvedBy -> … (event)  [2026-06-30T07:39:17Z]
  cites -> 532e2b4d… (source)  [2026-06-30T07:39:17Z]
```

```bash
vouch audit
```

```
… proposal.claim.create   by a               objects=['20260630-073841-d722ac24']
… proposal.claim.approve  by alice-reviewer   objects=['20260630-073841-d722ac24', 'auth-uses-jwts-…']
```

The audit log is the authoritative history: who proposed, who approved, when,
and citing what. It's append-only and committed alongside the claims.

## 8. Commit it

Approved artifacts are plain files, so git is your backup, sync, and second
audit log:

```bash
git add .vouch && git commit -m "kb: approve auth-uses-jwt"
```

From here the claim diffs cleanly in PRs, travels with the repo, and any future
session — yours or an agent's — reads the agreed answer instead of guessing.

## Keeping it true over time

Decisions change. vouch has lifecycle commands so the record stays honest
instead of silently drifting (each is recorded as an audit event):

```bash
vouch confirm <claim-id>                 # re-affirm a still-true claim, bumps last_confirmed_at
vouch supersede <old-id> <new-id>        # mark an old claim replaced by a newer one
vouch contradict <claim-a> <claim-b>     # record that two claims conflict
vouch archive <claim-id>                 # retire a claim, kept for history
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `forbidden_self_approval` | The proposer is trying to approve their own claim. | Approve under a different `VOUCH_AGENT`, or set `review.approver_role: trusted-agent` for solo use. |
| `error: … run vouch init in your project root` | No `.vouch/` found walking up from the cwd. | Run `vouch init`, or `cd` into the project that has the KB. |
| `source add` printed the same id twice | Sources dedupe by content hash. | Working as intended — identical bytes are one source. |
| `vouch show <id>` says "proposal not found" | The proposal was already approved or rejected. | Check `vouch pending` for live ids, or `vouch audit` for decided ones. |

## Next steps

- [Give your coding agent a reviewed memory](connect-coding-agent.md) — wire
  this KB into Claude Code, Codex, or Cursor so the agent reads and proposes.
- [Share a knowledge base across machines and teammates](share-a-knowledge-base.md)
  — bundle this KB and import it elsewhere.
- [The object model](../object-model.md) — claims, pages, entities, relations,
  sources, and how they link.
