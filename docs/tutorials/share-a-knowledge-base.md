# Share a knowledge base across machines and teammates

A reviewed KB is only as useful as the people and agents that can read it. By
the end of this tutorial you'll bundle a KB into a single portable file, preview
exactly what importing it would change, merge it into a second KB without
clobbering anything, and read the team-level observability that tells you who's
proposing and who's approving. The review gate does the rest: on a shared KB, no
contributor can rubber-stamp their own writes.

- **Time:** about 20 minutes
- **Cost:** none — no API keys, no server
- **You'll need:** a vouch KB with at least one approved claim (the
  [first tutorial](first-knowledge-base.md) gets you there)

vouch is local-first by design — there's no hosted service. Sharing is just
moving reviewed, plain-text files around, and git or a bundle is the transport.

## 1. Export a portable bundle

Bundle the durable KB — claims, pages, sources, config, audit trail — into one
`.tar.gz`:

```bash
vouch export --out kb.tar.gz
```

```json
{
  "bundle_id": "101a22c84e802e5238bb9e4618de65632c830f57162e9842d8d749bd06299fd3",
  "files": 9,
  "out": "kb.tar.gz"
}
```

The `bundle_id` is a content hash of everything inside, so two bundles are
identical iff their ids match. Nothing from `proposed/` (local, gitignored)
goes in — only reviewed artifacts travel.

## 2. Verify the bundle is intact

Before you hand a bundle to anyone, confirm every file matches its recorded
hash:

```bash
vouch export-check kb.tar.gz
```

A clean check means the bundle wasn't truncated or tampered with in transit.

## 3. Preview the import — no writes

On the receiving machine (or a teammate's clone), see what importing *would*
change before touching anything:

```bash
vouch import-check kb.tar.gz
```

`import-check` diffs the bundle against the destination KB and reports what's
new, what's identical, and what conflicts — without writing a byte. This is the
"read the PR before you merge it" step.

## 4. Apply it — conflict-safe

```bash
vouch import-apply kb.tar.gz
```

```json
{
  "bundle_id": "101a22c84e802e5238bb9e4618de65632c830f57162e9842d8d749bd06299fd3",
  "identical": ["config.yaml", "sources/…/content"],
  "on_conflict": "skip",
  "skipped_conflicts": ["claims/vouch-starter-reviewed-knowledge.yaml", "…"]
}
```

By default, files that already exist with different content are **skipped**, not
overwritten — importing never silently clobbers a teammate's reviewed claim.
Everything new from the bundle lands; everything identical is a no-op; conflicts
are reported for a human to resolve. After import, the claims are immediately
recallable:

```bash
vouch search "JWT"
```

```
claim/auth-uses-jwts-in-the-authorization-header	Auth uses JWTs in the Authorization header.  (substring)
```

> **git is the other transport.** Because approved artifacts are plain
> YAML/markdown, a teammate can just `git pull` the repo and the `.vouch/`
> claims come with it — reviewed in the same PR as the code. Bundles are for
> moving a KB *between* repos or onto a machine that isn't a git clone.

## 5. Why the gate is a team property

On a solo KB, the self-approval check feels like a formality. On a shared KB
it's the safety property: it guarantees that every durable claim was seen by a
*second* identity. An agent can't approve its own output; a teammate can't
merge their own claim unreviewed. The actor breakdown makes the separation
visible:

```bash
vouch metrics
```

```
  review gate
    proposals created   3
    approved / rejected 1 / 0
    approval rate       100.0%
    pending now         2

  actors (proposed / approved / rejected / confirmed)
    a                  2 / 0 / 0 / 0
    alice-reviewer     0 / 1 / 0 / 0
    vault-sync         1 / 0 / 0 / 0
```

Proposers and approvers are different rows — that's the gate working. `metrics`
reads purely from the audit log and the artifact files (no new state), and
`--json` / `--prometheus` give you a stable shape for a dashboard or a
textfile-collector sidecar.

For the day-to-day queue, `stats` is the lighter view:

```bash
vouch stats
```

```
  pending: 2 proposal(s)
  review (last 30d): 1 approved, 0 rejected, 0 expired
  approval rate: 100.0%
  citations: 2/2 claims with valid citations (100.0%)
```

## 6. Keep shared copies honest

A bundle is a snapshot. To keep two KBs converging:

- **Re-export** after a review session and re-share — the `bundle_id` tells
  recipients instantly whether they already have this exact state.
- **Commit `.vouch/`** so the canonical copy lives in git and bundles are only
  for out-of-band transfer.
- **Run `vouch doctor`** on the receiving side after a big import — it verifies
  source hashes, finds dangling references, and reports drift.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `import-apply` skipped claims | Same id, different content — a real conflict. | Inspect both with `vouch show` / `vouch diff`, decide, then `supersede` the loser. |
| `export-check` fails | Bundle truncated or modified in transit. | Re-export and re-transfer; never edit a bundle by hand. |
| Imported claims don't appear in search | Derived index is stale. | `vouch index` (or `vouch reindex`) to rebuild `state.db`. |
| Teammate can approve their own claim | `review.approver_role: trusted-agent` is set. | Remove it from `config.yaml` for shared KBs so the gate enforces two identities. |

## Next steps

- [Give your coding agent a reviewed memory](connect-coding-agent.md) — point
  every teammate's agent at the shared KB.
- [Edit your KB as markdown in Obsidian](edit-in-obsidian.md) — review and
  extend the KB outside the terminal.
- [The review gate in depth](../review-gate.md) — the full set of policies the
  gate enforces.
