---
description: Narrate the vouch digest — pending reviews, decisions, stale claims, due followups
---

# /vouch-standup

Give the user a morning briefing from the KB, then get out of the way.

Steps:

1. Call the `kb_digest` MCP tool (or run `vouch digest --format markdown`
   via Bash if MCP is unavailable). Default window is fine unless the user
   names one.
2. Narrate it in four short lines, leading with counts: pending proposals
   (oldest first — nudge `vouch review` if any), decisions since the window
   start, stale claims worth a `kb_confirm` or supersede, and followups due
   (with owners).
3. If a followup in the list is done, offer to file the close — a
   `kb_propose_page` with the page's id as `slug_hint` and
   `followup_status: "done"` — but only on explicit confirmation.

Read-only otherwise. Never call `kb_approve`; reviewing is the human's job.
