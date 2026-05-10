# Governance

vouch is pre-1.0 and small. This document describes how decisions get made
*today*; expect it to evolve as the project grows.

## Roles

**Maintainers** have commit access on `main` and merge rights. Today there
is one maintainer. Adding a maintainer requires unanimous agreement of
existing maintainers.

**Contributors** are anyone who opens an issue or PR. No formal status.

**Approvers** is a vouch-internal term meaning whoever is allowed to run
`vouch approve` on a given `.vouch/` deployment — that's an operational
concept inside the tool, not a project role.

## Decisions

Three kinds of decisions, three different bars:

1. **Day-to-day code changes** — bug fixes, refactors, doc tweaks,
   new tests. A single maintainer review suffices. PRs from
   non-maintainers need a maintainer reviewer.

2. **Surface changes** — anything touching the object model, the `kb.*`
   method surface, the on-disk layout in `.vouch/`, the bundle format,
   or the audit-log shape. These require a **[VEP](proposals/README.md)**
   (Vouch Enhancement Proposal): a written design doc filed under
   `proposals/`, discussed in public, and explicitly accepted by a
   maintainer before implementation. If two or more maintainers exist,
   acceptance requires majority.

3. **Project-level changes** — changing this document, changing the
   license, electing/removing maintainers, changing the project
   direction. Requires consensus among all maintainers and a 14-day
   public comment window on a PR or issue.

## Disagreements

If contributors disagree on a PR, the assigned maintainer makes the call.
If contributors disagree with a maintainer, escalate by opening an issue
labelled `governance:` and naming the dispute. Maintainers respond
publicly within two weeks.

There is no separate "BDFL" or steering committee at this scale. If the
project grows past one maintainer, this document will be revised to add
quorum rules and a written tiebreaker.

## Code of Conduct

All project spaces are governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Conduct enforcement is a maintainer decision, not a community vote.

## License & contribution terms

vouch is MIT-licensed. Contributions are assumed to be offered under the
same terms — there is no CLA. If your employer requires a CLA, raise it on
your PR and we'll work it out.

## Forking

The license permits forks. We'd appreciate a heads-up issue if you fork
with the intent to publish under a similar name, so users don't get
confused — that's a courtesy, not a rule.
