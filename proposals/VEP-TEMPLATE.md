---
vep: NNNN
title: Short descriptive title
author: your-handle
status: draft
created: YYYY-MM-DD
landed-in: ""
supersedes: []
superseded-by: ""
---

# VEP-NNNN: Title

## Summary

One paragraph. What does this proposal change?

## Motivation

What problem are we solving? Be concrete. Examples beat abstractions —
"users report that X" or "the audit log misses Y" lands better than
"improve correctness". If there's a real-world incident, link it.

## Proposal

What's the change, at the level of the surface and on-disk layout?

If this adds a method, describe its parameters and result. If this
adds a field, name it and give a type. If this changes file layout,
draw the directory tree before and after.

## Design

Implementation-level detail. Enough that someone other than the author
can build it. Pseudo-code and small diagrams welcome.

## Compatibility

- What existing `.vouch/` directories break?
- Is a migration needed? Outline it.
- Does this change the bundle format? If yes, bump the version.
- Does this change the `kb.capabilities` shape?

## Security implications

If the change touches the review gate, the audit log, or any
trust boundary, walk through what new attacks become possible — and
what mitigations apply.

## Performance implications

If there's a hot path (search, context, index rebuild), say what
changes and ideally include a back-of-envelope estimate.

## Open questions

Things you haven't decided. The PR reviewers will weigh in.

## Alternatives considered

What else did you think about? Why did you not pick it? Be honest —
"laziness" is a fine answer.

## References

Issues, prior discussion, external prior art.
