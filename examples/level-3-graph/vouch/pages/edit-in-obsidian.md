---
id: edit-in-obsidian
title: Edit in Obsidian
type: workflow
status: active
claims:
- vouch-starter-reviewed-knowledge
entities: []
sources:
- be7aec64b0fc803a33cb3d610f67ae95e636877db20231ef72440a7cbe6b69d2
tags:
- vouch
- onboarding
- obsidian
metadata: {}
created_at: '2026-06-30T02:31:37.687271Z'
updated_at: '2026-06-30T02:31:37.687275Z'
---
# Edit in Obsidian

Vouch's pages are plain markdown with YAML frontmatter -- your knowledge
base is already an Obsidian-compatible vault. To edit pages in your own
Obsidian vault:

1. Run `vouch sync --vault ~/Obsidian/YourVault` once to mirror approved
   pages and claims under `<vault>/vouch/`.
2. Open `<vault>/vouch/pages/<id>.md` in Obsidian and edit it.
3. Re-run `vouch sync --vault ~/Obsidian/YourVault` to file your edits as
   page-edit proposals in `.vouch/proposed/`.
4. Review and approve with `vouch approve <id>`. The next sync mirrors the
   approved version back into the vault.

Claims appear as stub markdown files under `<vault>/vouch/claims/`; pages
that cite them are linked via Obsidian `[[wikilink]]` syntax so the graph
view connects them. Use `--watch` to keep a polling loop alive while you
edit.
