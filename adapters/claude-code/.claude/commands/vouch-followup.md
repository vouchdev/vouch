---
description: Propose a dated followup the vouch digest will surface until closed
---

# /vouch-followup

File "$ARGUMENTS" as a `followup` page proposal — a commitment with a due
date that `vouch digest` lists until it's closed. Requires the company-brain
page kinds (`vouch init --template company-brain`).

Steps:

1. Extract what's owed, by whom, and when. If no due date is stated, ask —
   an undated followup never surfaces.
2. Call `kb_propose_page` with `page_type: "followup"`, a short imperative
   title, and `metadata`: `due_at` (ISO date), `followup_status: "open"`,
   `owner` when known. Put context in the body; cite a source id if the
   commitment came from a registered conversation or document.
3. To close or move one later: re-propose the same page (`slug_hint: <page
   id>`) with `followup_status: "done"` (or a new `due_at`) — status changes
   go through the gate like any other edit.
4. Report the proposal id and the due date filed.

Never call `kb_approve`.
