# Edit your KB as markdown in Obsidian

Not every edit wants to happen in a terminal. By the end of this tutorial your
reviewed KB will mirror into an Obsidian-compatible vault — approved pages and
claims as linked markdown, so the graph view works — and edits you make in
Obsidian will come back as review-gated proposals, citing the exact bytes you
changed. The gate stays in the loop the whole time: editing a file in your
vault never writes straight into the KB.

- **Time:** about 15 minutes
- **You'll need:** a vouch KB with at least one approved page or claim (the
  [first tutorial](first-knowledge-base.md) gets you there). Obsidian itself is
  optional — any markdown editor works; Obsidian just renders the wikilinks and
  graph.

Sync is bidirectional, and the two directions do very different things:

- **backward (KB → vault):** approved pages and claims mirror *out* into the
  vault as readable, linked markdown.
- **forward (vault → KB):** edits you make in the vault come *back* as
  page-edit proposals in `proposed/`, which still have to clear the review gate.

## 1. Mirror the KB into a vault

Pick a folder for the vault and mirror approved artifacts into it:

```bash
mkdir -p ~/my-vault
vouch sync --vault ~/my-vault --direction backward
```

```
  ↓ pages/edit-in-obsidian.md  (mirrored)
  ↓ claims/vouch-starter-reviewed-knowledge.md  (mirrored)
Done — 1 pages and 1 claims mirrored, 0 proposals filed.
```

Everything lands under `<vault>/vouch/`:

```
~/my-vault/vouch/
  pages/edit-in-obsidian.md
  claims/vouch-starter-reviewed-knowledge.md
  .sync-state.json
```

Approved pages mirror as full markdown. Approved claims get a markdown stub
under `claims/` with Obsidian wikilink backlinks to the pages that cite them —
so when you open the vault in Obsidian, the graph view connects claims to the
pages they support.

## 2. Open it in Obsidian

Point Obsidian at `~/my-vault` (Open folder as vault). Browse `vouch/pages/`,
open the graph view, and you'll see the reviewed knowledge as a navigable web of
linked notes. This is a read-friendly window onto the same `.vouch/` you've been
driving from the CLI — nothing here is a second copy of the truth, it's a
projection of it.

## 3. Edit a page — the change becomes a proposal

Edit a mirrored page in Obsidian (or any editor). Add a line to a page under
`<vault>/vouch/pages/`:

```bash
echo "Added a new line while editing in Obsidian." >> ~/my-vault/vouch/pages/edit-in-obsidian.md
```

Now run sync the other direction:

```bash
vouch sync --vault ~/my-vault --direction forward
```

```
  ↑ pages/edit-in-obsidian  (proposal filed)
Done — 0 pages and 0 claims mirrored, 1 proposals filed.
```

Your edit did **not** write into the KB. It became a proposal:

```bash
vouch pending
```

```
• 20260630-074121-a44eb574  [page]  by vault-sync
    Edit in Obsidian
```

The proposal cites a `vault:<relpath>` source pointing at the file you changed,
so the reviewer can see exactly which bytes triggered it — the gate isn't
reviewing a vague "something changed," it's reviewing your specific edit.

## 4. Review the edit — the gate, as always

The proposal sits in the queue under the `vault-sync` actor until a human
accepts it. Because the proposer is `vault-sync` and not you, your approval
satisfies the gate:

```bash
vouch show 20260630-074121-a44eb574       # see the diff your edit produced
vouch approve 20260630-074121-a44eb574 --reason "good clarification"
# or:
vouch reject 20260630-074121-a44eb574 --reason "not accurate"
```

Only after approval does the page change land in the durable KB — and then a
backward sync mirrors the now-canonical version back out to the vault. Edit
freely; nothing is true until it's reviewed.

## 5. Keep them in sync continuously

Run both directions at once, and add `--watch` to keep a polling loop alive
while you work in Obsidian:

```bash
vouch sync --vault ~/my-vault --direction both --watch
```

Re-runs are idempotent: only real edits become proposals, and only genuinely
changed approved artifacts re-mirror. A no-op sync writes nothing.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Missing option '--vault'` | `sync` needs an explicit vault path. | Pass `--vault <dir>`. |
| Edited a vault file, no proposal | Ran backward, not forward. | `vouch sync --vault <dir> --direction forward`. |
| Edit went straight in with no review | You edited `.vouch/` directly, not the vault. | Edit under `<vault>/vouch/`; the KB files are the reviewed output, not the input. |
| Vault page reverted after approve | A backward sync re-mirrored the canonical version. | Working as intended — the approved KB is the source of truth. |

## Next steps

- [Share a knowledge base across machines and teammates](share-a-knowledge-base.md)
  — move the reviewed KB beyond one machine.
- [The object model](../object-model.md) — pages vs. claims, and how citations
  link them.
- [The review gate in depth](../review-gate.md) — every policy the gate
  enforces, including on vault-sourced proposals.
