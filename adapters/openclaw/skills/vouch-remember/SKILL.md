---
name: vouch-remember
description: File something the user wants remembered as a cited, review-gated proposal
---

# /vouch-remember

Turn "$ARGUMENTS" into durable KB knowledge — proposed, never self-approved.

Steps:

1. Call `kb_register_source` with the user's exact wording as `content`
   (`source_type: "message"`, a short descriptive `title`). Sources are
   evidence intake; registering one writes no knowledge.
2. Distill the durable fact(s) into one `kb_propose_claim` each, citing the
   registered source id in `evidence`. Keep each claim one sentence,
   present tense, self-contained.
3. If the fact is about a person, org, or project the KB doesn't know yet,
   also file `kb_propose_entity` (and `kb_propose_relation` to link it).
4. Report the proposal id(s) and remind the user: pending items are invisible
   to retrieval until a human runs `vouch approve <id>` or `vouch review`.

Never call `kb_approve` — the human at the gate decides what the KB believes.
