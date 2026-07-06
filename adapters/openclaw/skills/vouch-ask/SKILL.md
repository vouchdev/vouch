---
name: vouch-ask
description: Answer a question from the vouch KB with citations, or say what's missing
---

# /vouch-ask

Answer "$ARGUMENTS" using only reviewed knowledge from the vouch KB. Every
statement in the answer must carry a `[claim-id]` or `[source-id]` citation.

Steps:

1. Call `kb_search` with `query: "$ARGUMENTS"`, then `kb_context` on the same
   query to assemble the working set.
2. If the results answer the question, write the answer citing every claim id
   you relied on. Use `kb_read_page` for typed record detail (contacts,
   project records, followups).
3. If the KB cannot support an answer, say so plainly and list the closest
   claims found. Do not fill gaps from your own knowledge — an uncited answer
   is worse than no answer.
4. Only when the user explicitly asks about in-flight knowledge, list
   `kb_list_pending` items — each labeled `UNREVIEWED` — after the cited
   answer, never mixed into it.

Never call `kb_approve`. Never restate pending proposals as facts.
