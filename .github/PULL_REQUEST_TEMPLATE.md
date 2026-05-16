<!--
Thanks for sending a PR! Before opening, please skim CONTRIBUTING.md.

Surface changes (object model, kb.* methods, on-disk layout, bundle
format, audit-log shape) need a VEP first; see proposals/README.md.
-->

## What changed

<!-- One paragraph. The *what*, briefly. -->

## Why

<!-- One paragraph. The *why* — what problem this solves, what it
     enables, or what it fixes. Link the issue if any. -->

## What might break

<!-- Be honest. For users with an existing .vouch/ directory:
     - Will any file move?
     - Will any field on disk change shape?
     - Will any kb.* method behave differently?
     If yes to anything, flag it as a breaking change. -->

## VEP

<!-- If this is a surface change, link the accepted VEP here.
     If you're not sure whether you needed one, ask. -->

## Tests

- [ ] `make check` passes locally (lint + mypy + pytest)
- [ ] New / changed behaviour has a test
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
