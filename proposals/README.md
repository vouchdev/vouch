# VEPs — Vouch Enhancement Proposals

Surface changes to vouch don't land as raw PRs. They land as **VEPs**:
short written designs filed here, discussed in public, and explicitly
accepted by a maintainer before any code is written.

> Yes, the irony of having a review gate for the project that's built
> around having a review gate is not lost on us.

## When you need a VEP

Open a VEP for anything that touches:

- The object model (Source/Evidence/Claim/Entity/Relation/Page/Session/Proposal/AuditEvent).
- The `kb.*` method surface — new methods, renamed methods, removed methods, changed parameter shapes.
- The on-disk layout under `.vouch/`.
- The bundle format or the audit-log shape.
- The default `config.yaml` semantics.
- Any new transport.

Day-to-day code changes — bug fixes, internal refactors, doc tweaks,
new tests, new lifecycle helpers that compose existing methods — go
straight to PR.

If you're not sure, file an issue first and ask.

## Process

1. **Copy** [`VEP-TEMPLATE.md`](VEP-TEMPLATE.md) to
   `proposals/VEP-NNNN-short-slug.md` where `NNNN` is the next free
   number (look at the list below).
2. **Fill in** Motivation, Proposal, Design, Compatibility, Open
   questions, Alternatives. Keep it short — most VEPs are 1-3 pages.
3. **Open a PR** against `main` adding the VEP file with status
   `draft`. The PR description should link to the design discussion if
   any.
4. **Iterate** in the PR review. Once a maintainer marks it
   `accepted`, you (or someone else) can implement it.
5. **Implementation** lands in separate PRs. When the implementation
   ships, update the VEP status to `final` in a follow-up PR.

A VEP is a *record*, not a *gate forever*. Accepted VEPs are not
re-litigated unless someone proposes a successor VEP that explicitly
supersedes them.

## States

- `draft` — under discussion
- `accepted` — agreed to implement; not yet shipped
- `final` — shipped in a released version
- `rejected` — discussed and declined; kept for history
- `superseded` — replaced by a later VEP; points to its successor
- `withdrawn` — author pulled it before a decision

## Index

| # | title | status | landed in |
|---|---|---|---|
| [0001](VEP-0001-review-gate.md) | Review gate | final | 0.0.1 |
| [0002](VEP-0002-jsonl-transport.md) | JSONL transport | final | 0.0.1 |
| [0003](VEP-0003-content-hashed-sources.md) | Content-hashed sources | final | 0.0.1 |
| [0005](VEP-0005-richer-scopes.md) | Richer scopes on Claim/Source | draft | — |
| [0004](VEP-0004-http-transport.md) | HTTP transport | draft | — |
| [0006](VEP-0006-dual-solve-web.md) | dual-solve web runner | draft | — |

## Numbering

Numbers are issued sequentially as VEPs are opened. Don't reserve
numbers; if your PR conflicts with someone else's on the next number,
the second-merged PR rebases and increments.

## Why not use issues?

GitHub issues are great for bug reports and feature requests. They're
not great for design records: they get closed, they sort by recency,
and they're not part of the repo. VEPs live in the repo, get reviewed
like code, and survive even if a discussion service goes away.
