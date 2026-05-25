# docs/

User-facing documentation. Organized by *what the reader is trying to
do*, not by *what part of the code it covers*.

If you're new, read in this order:

1. [getting-started.md](getting-started.md) — install, init, first
   proposal, first approval.
2. [object-model.md](object-model.md) — what Claim / Source / Page /
   Entity / Relation actually mean, with examples.
3. [review-gate.md](review-gate.md) — how the propose → approve flow
   works, including edge cases.
4. [transports.md](transports.md) — wiring vouch into your LLM host or
   into shell scripts.
5. [retrieval.md](retrieval.md) — how `kb.search` and `kb.context`
   pick what to return.
6. [bundles.md](bundles.md) — exporting and importing KBs between
   repos.
7. [multi-agent.md](multi-agent.md) — running several agents against
   one KB without stepping on each other.
8. [faq.md](faq.md) — questions we keep getting.
9. [embeddings.md](embeddings.md) — semantic retrieval (primary backend)

### How docs relate to other directories

| Where | Audience |
|---|---|
| `README.md` (root) | newcomer, quick-pitch |
| `docs/` | end users and operators |
| `SPEC.md` + `spec/` | implementers of alternative servers, auditors |
| `proposals/` | people evolving the protocol |
| `schemas/` | tooling that validates artifacts |
| `examples/` | learning by reading real shapes |
| `templates/` | learning by copy-paste |

Docs are *explainers*. Spec is *normative*. When they disagree, the
spec wins; please file an issue.
