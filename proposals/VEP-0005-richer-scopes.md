---
vep: 0005
title: Richer scopes on Claim/Source
author: dripsmvcp
status: draft
created: 2026-05-26
landed-in: ""
supersedes: []
superseded-by: ""
---

# VEP-0005: Richer scopes on Claim/Source

## Summary

Replace the single `scope` enum on `Claim` and `Source` with a
structured scope of `(visibility, project, agent)`, so a KB shared
across agents and projects can express *who-sees-what*. Out-of-scope
artifacts are filtered out of retrieval (`kb.search`, `kb.context`).
Existing KBs keep working unchanged: a bare `scope: project` string
still parses, and the default viewer context behaves like today.

## Motivation

`Claim` and `Source` each carry a single `scope: Scope` field today
(`src/vouch/models.py:141,195`), where `Scope` is a flat visibility tier:

```python
class Scope(StrEnum):
    PRIVATE = "private"
    PROJECT = "project"
    TEAM = "team"
    PUBLIC = "public"
```

That answers "how widely visible" but not "*which* project" or "*which*
agent." With deterministic multi-agent sync now landed (#90 / #91), a
single `.vouch/` gets populated by several agents working on several
projects. The moment two projects share a KB, retrieval can't tell them
apart: `vouch context "auth"` for project A surfaces project B's claims,
and one agent's `private` scratch claims are indistinguishable from
another's because `private` has no owner.

Concretely, today you cannot say:

> "This claim belongs to project `billing`, was authored by agent
> `claude-cli`, and should only show up in that project's context."

ROADMAP 0.2 lists richer scopes and marks it **[VEP]**.

## Proposal

Introduce a structured scope object and use it on `Claim` and `Source`.
The existing flat enum is renamed `Visibility` (its values are
unchanged) and becomes one field of the new object:

```python
class Visibility(StrEnum):       # was: Scope (values unchanged)
    PRIVATE = "private"
    PROJECT = "project"
    TEAM = "team"
    PUBLIC = "public"

class ArtifactScope(BaseModel):
    visibility: Visibility = Visibility.PROJECT
    project: str | None = None   # None = not bound to a specific project
    agent: str | None = None     # None = not bound to a specific agent
```

`Claim.scope` and `Source.scope` become
`scope: ArtifactScope = Field(default_factory=ArtifactScope)`.

On-disk YAML changes from:

```yaml
scope: project
```

to:

```yaml
scope:
  visibility: project
  project: billing
  agent: claude-cli
```

### Retrieval filtering

Retrieval gains a **viewer context** `(project, agent)`, supplied (in
priority order) by an explicit request param, then `VOUCH_PROJECT` /
`VOUCH_AGENT` env, then a `retrieval.scope` block in `config.yaml`,
else empty. An artifact is *visible* to a viewer iff:

| visibility | visible when …                                              |
|------------|-------------------------------------------------------------|
| `public`   | always                                                      |
| `team`     | always (team-wide within this KB)                           |
| `project`  | `scope.project is None` **or** `scope.project == viewer.project` |
| `private`  | `scope.agent == viewer.agent` (owner only)                  |

`_retrieve` (in `context.py`) and the `kb.search` handlers apply this
filter to hits before returning. The **default** viewer context (no
project, no agent) sees `public` + `team` + unbound `project` artifacts
and hides every `private` and every project-bound artifact owned by a
*different* project — chosen so a fresh single-agent KB behaves exactly
as it does today (everything defaults to `project` with `project=None`,
which stays visible).

## Design

- **Models** (`models.py`): rename `Scope` → `Visibility`; add
  `ArtifactScope`; switch `Claim.scope` / `Source.scope` to it. Keep a
  module-level `Scope = Visibility` alias for one release to avoid
  breaking imports.
- **Back-compat parsing**: a `field_validator("scope", mode="before")`
  on both models coerces a bare string (the old form) into
  `{"visibility": <string>}`. So every existing `claims/*.yaml` and
  `sources/*/meta.yaml` reads without a migration step.
- **Viewer context** helper, e.g. `scoping.viewer_from(config, params,
  env) -> ArtifactScope`-like `(project, agent)`; one place computes it
  for all transports.
- **Filtering** lives as a post-retrieval pass first (`[h for h in hits
  if visible(h.scope, viewer)]`) — simple, transport-agnostic, and easy
  to test. A later optimization can push project/visibility into the
  SQL `WHERE` in `index_db` if profiling warrants (see Performance).
- **Config**: optional `retrieval.scope: {project, agent}` in
  `config.yaml`. (This is a `config.yaml` semantics addition — called
  out under Compatibility.)
- **Capabilities**: add a `scoping` flag/section to `kb.capabilities`
  so clients can detect that this server filters by scope.

## Compatibility

- **Existing `.vouch/` directories**: read unchanged thanks to the
  string→object validator. On next write the artifact is re-emitted in
  the new object form (lazy migration); a `vouch migrate` pass can
  rewrite eagerly but is not required.
- **Bundle format**: bundles produced after this change carry the scope
  *object*. An older vouch importing a new bundle would see an object
  where it expects a string and fail schema validation — a
  forward-incompatibility. Proposal: bump the bundle `spec` version and
  have new importers accept both forms (the validator already does).
- **`kb.capabilities` shape**: additive (`scoping` section). Existing
  consumers are unaffected.
- **JSON Schemas** (`schemas/`) regenerate: `claim`/`source` `scope`
  changes from an enum string to an object. Downstream schema consumers
  must update.
- **`kb.*` method surface**: unchanged method *names*; `kb.search` /
  `kb.context` gain optional `project` / `agent` params (additive,
  defaulted), and results may now be *filtered* — a behavior change
  reviewers should weigh (a viewer with a project set sees fewer hits).

## Security implications

**Scope is a retrieval/relevance filter, not an access-control or
confidentiality boundary, and the VEP must say so loudly.** A `.vouch/`
is plaintext YAML/Markdown in (usually) a git repo. Anyone who can read
the directory reads every artifact regardless of scope —
`scope.visibility = private` means "excluded from other agents' default
context," **not** "secret" or "encrypted." Treating `private` as a
secret store would be a serious misuse.

Implications:

- No new attack surface on the review gate or audit log — scope does not
  gate writes or approvals, only what retrieval *returns*.
- Filtering must **fail closed for `private`**: a missing/empty viewer
  agent must not match someone else's `private` artifact (default viewer
  has `agent=None`, and `None == "someone"` is false — good), but this
  needs an explicit test so a future refactor can't flip it to fail-open.
- Sync (#90/#91) must not be assumed to respect scope as a trust
  boundary across machines — it copies files, and the files carry their
  contents in the clear. Documented in `spec/`.

## Performance implications

Post-retrieval filtering is `O(hits)` with a cheap per-hit comparison —
negligible against the search itself. The only artifacts whose scope
must be known are the ones already fetched. If a future workload makes
this hot (e.g. a viewer scoped to a tiny project in a huge KB, where we
over-fetch then discard most hits), push `visibility`/`project` into the
`embedding_index` / FTS rows and filter in SQL. Out of scope for v1.

## Open questions

- **`team` semantics.** With one KB == one team, `team` and a
  project-unbound `project` are nearly identical. Is `team` worth keeping
  distinct from `public` here, or does it only matter once cross-KB sync
  has a notion of "team"? Leaning: keep the enum value, treat
  team-wide == KB-wide for now.
- **Where to filter.** Post-retrieval pass (proposed) vs SQL `WHERE` in
  `index_db`. Post-pass is simpler and transport-uniform; SQL is faster
  at scale. Start with the pass?
- **Viewer-context source of truth.** Request param vs env
  (`VOUCH_PROJECT`/`VOUCH_AGENT`) vs `config.yaml` — proposed precedence
  is param > env > config. Agree?
- **Entities / relations / pages.** The issue scopes only Claim and
  Source. Do Entity/Relation/Page need scope too (a project-scoped page
  leaking into another project's context is the same problem)? Propose
  deferring to a follow-up VEP unless reviewers want it now.
- **Sync interaction.** Should `vouch sync-apply` partition by project
  scope, or import everything and rely on retrieval filtering? (#90/#91.)

## Alternatives considered

- **Keep the single enum, use `tags` for project/agent.** Tags already
  exist and need no schema change. Rejected: untyped, unvalidated, and
  every consumer would reinvent the "is this tag a project?" convention;
  filtering would be string-soup. A typed field is the point.
- **ACLs / encryption per scope.** Wrong model for a git-backed plaintext
  KB and a large surface; confidentiality isn't what this issue asks for.
- **Per-project subdirectories under `.vouch/`.** Physically partitions
  artifacts, but it's a much bigger on-disk-layout change, complicates
  content-addressing and sync, and doesn't express `visibility` or
  `agent`. Rejected for v1.
- **A free-form `scope: dict`.** Maximum flexibility, zero validation.
  Rejected for the same reason as tags.

## References

- Issue [#100](https://github.com/vouchdev/vouch/issues/100)
- Deterministic sync: #90 / #91 (the change that makes this matter)
- [VEP-0001: Review gate](VEP-0001-review-gate.md) — unaffected by scope
- [ROADMAP.md](../ROADMAP.md) — 0.2 line item, marked [VEP]
