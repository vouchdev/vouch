# playbook-vault-sync

Bidirectional Obsidian vault sync — mirror approved vouch pages and claims
into an Obsidian-style markdown vault (backward), and turn vault page edits
into review-gated page-edit proposals (forward). Nothing bypasses the
review gate: a vault edit becomes a *proposal*, not a silent write.

This is a **playbook** because the real-world mode (`--watch`) is a
long-lived polling loop that never exits on its own. `run.sh` runs one
non-watch backward+forward cycle so you can see the whole shape in a few
seconds, then prints the `--watch` command with a "not auto-run" notice.

## Run it:

```bash
VOUCH=/path/to/vouch bash run.sh
# or, with vouch on your PATH:
./run.sh
```

The script builds a throwaway KB in `$(mktemp -d)`, mirrors it into a
throwaway vault, edits a mirrored file, syncs the edit back as a proposal,
and cleans both up on exit. `VOUCH_AGENT=example-agent` is exported so the
proposer is attributed consistently.

## The flow

1. **init** — a fresh KB seeds one approved page (`edit-in-obsidian`,
   status `active`) and one approved claim.
2. **backward sync** (`vouch sync --vault $VAULT --direction backward`) —
   mirrors every approved page into `<vault>/vouch/pages/<id>.md` and every
   approved claim into `<vault>/vouch/claims/<id>.md` with Obsidian
   `[[wikilink]]` backlinks. vouch records a content hash per mirrored file
   in `<vault>/vouch/.sync-state.json`.
3. **edit a mirrored page** — append a paragraph to
   `vouch/pages/edit-in-obsidian.md`, the way you would in Obsidian.
4. **forward sync** (`vouch sync --vault $VAULT --direction forward`) —
   vouch notices the file's hash no longer matches `.sync-state.json`,
   registers a source with `locator="vault:pages/edit-in-obsidian.md"` so
   the gate sees the *exact bytes* you edited, and files a **page-edit
   proposal**. The KB page on disk is untouched.
5. **`vouch pending`** — the edit is now a pending proposal awaiting review,
   not a durable change.
6. **JSONL `kb.list_pending` + `kb.read_page`** — the pending proposal
   carries the edited body and an extra `vault:`-cited source; reading the
   approved page back shows its body is still the *original* — proof the
   review gate held.

The Vault Edit Proposal (VEP) shape: every forward edit is one
`page-edit` proposal citing a `vault:<relpath>` source. A reviewer runs
`vouch approve <id>` (in the real flow, a different actor than the
proposer — vouch forbids self-approval). The next backward sync mirrors
the now-approved version back into the vault, closing the loop.

`--watch` keeps a polling loop alive (`vouch sync --vault <path> --watch
--poll 2`), re-running forward+backward every `--poll` seconds. It blocks
until Ctrl-C, which is why this example does not auto-run it.

## Real output excerpt

```text
==============================================================
 2. backward sync (KB -> vault): mirror approved pages/claims
==============================================================
      ↓ pages/edit-in-obsidian.md  (mirrored)
      ↓ claims/vouch-starter-reviewed-knowledge.md  (mirrored)
    Done — 1 pages and 1 claims mirrored, 0 proposals filed.

==============================================================
 4. forward sync (vault -> KB): the edit becomes a PROPOSAL
==============================================================
      ↑ pages/edit-in-obsidian  (proposal filed)
    Done — 0 pages and 0 claims mirrored, 1 proposals filed.

==============================================================
 5. the page edit is sitting in the review queue, not applied
==============================================================
    • 20260630-023118-8f9a0be6  [page]  by vault-sync
        Edit in Obsidian
```

The pending proposal's payload carries the edited body and a second,
`vault:`-cited source id, while `kb.read_page` on `edit-in-obsidian`
returns the unchanged original body — the write never reached the KB
without review:

```json
{"id": "r1", "ok": true, "result": [{"kind": "page", "proposed_by": "vault-sync",
  "payload": {"id": "edit-in-obsidian",
    "body": "...New paragraph added in the vault by alice-example.\n",
    "sources": ["be7aec64...", "99aa04d4..."]}, "status": "pending"}]}
{"id": "r2", "ok": true, "result": {"id": "edit-in-obsidian",
  "body": "...edit.\n", "status": "active", "sources": ["be7aec64..."]}}
```

## Methods demonstrated:

- **`kb.propose_page`** — the forward sync files the vault edit as a
  page-edit proposal (vouch's `proposals.propose_page`), citing the exact
  `vault:<relpath>` bytes.
- **`kb.list_pending`** — surfaces the filed proposal in the review queue
  over the JSONL transport.
- **`kb.read_page`** — reads the still-approved original, proving the page
  on disk is unchanged until a reviewer approves the proposal.
