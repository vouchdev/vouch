---
description: Recall what the project's vouch KB knows about a topic
---

# /vouch-recall

Recall what the KB already knows about a topic and put it to work in the
conversation. Retrieval is hybrid (embedding → fts5 → substring, per
`retrieval.backend`), so query by meaning — the topic as a problem
statement — not by exact strings. Read-only: do not write anything.

Steps:

1. Call `kb_context` with `task: "$ARGUMENTS"`.
2. If the hits are empty or weak, retry `kb_context` up to two times with
   reformulations: synonyms, the entity/file/tool names involved, or the
   problem behind the phrasing. Different wording reaches different claims.
3. Merge the results, then call `kb_neighbors` on the most relevant claim
   id to pull directly related claims, relations, and entities the query
   wording missed.
4. Answer the user's actual question *using* the recalled claims — weave
   them in as the authoritative baseline, citing each by claim id plus the
   source ids it cites. Where a recalled claim contradicts what you were
   about to say, the approved claim wins; flag the conflict explicitly.
5. If nothing relevant is approved in the KB, say exactly that in one
   line, then name the gap the user could fill with
   `/vouch-propose-from-pr` or `kb_propose_claim`. Never present guesses
   as recall.

Be terse. The KB is meant to remove ambiguity, not pad it.
