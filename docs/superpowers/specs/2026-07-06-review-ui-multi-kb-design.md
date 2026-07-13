# multi-project aggregation — design (implemented in vouch-ui)

date: 2026-07-06
status: SHIPPED on `feat/multi-project-aggregation` in ~/Dev/plind-junior/vouch-ui
(repo vouchdev/webApp). an earlier draft of this spec targeted vouch's built-in
`review-ui` (`src/vouch/web/`); the user redirected — the vouch repo's web
folder stays untouched, aggregation lives in the separate vouch-ui console.
that server-side branch survives unpushed as `feat/review-ui-multi-kb` in the
vouch repo, reference only.

## problem

Every vouch KB is project-local and every `vouch serve --transport http`
endpoint serves exactly one KB. vouch-ui could connect to one endpoint at a
time, so reviewing three projects meant three browser tabs. The feature: one
console that aggregates all connected projects.

## design

Transport needed zero changes: the dev-server proxy already routes each
request by its `X-Vouch-Target` header, so multi-endpoint is purely an
app-model concern.

* **Projects.** `ConnectionContext` now holds a list of endpoints
  (`vouch-ui.connections.v2` storage, migrating the v1 single-endpoint key),
  each with its own capabilities and health, plus a persisted **scope** —
  `all` or one endpoint. `connect()` validates before adding; re-adding an
  endpoint updates it in place (token/label refresh). `active` = the scoped
  project (or first), for single-target surfaces.
* **Fan-out.** `useFanout(key, method)` (src/lib/fanout.ts) runs one kb.* read
  per scoped project via useQueries, cache-keyed `[...key, endpoint]` — prefix
  invalidation hits the whole fan-out; optimistic updates target one slice.
  Projects whose caps haven't loaded are queried optimistically, same as the
  old single-endpoint behaviour.
* **Views.** Pending / Review / Claims / Browse merge rows across projects
  with a per-row project badge; approve / reject / summarize / merge route to
  the owning endpoint so each decision lands in that project's own review
  gate + audit log. Merge is constrained to one project. Stats stacks one
  panel per project. Chat + artifact drawer target the active project.
  Compile is per-project and offered only when the scope names one.
* **Shell.** Scope switcher (All projects / each project), manage-projects
  dialog (add/remove, health dots), nav badges sum the scope. Proposal ids
  are unique only per KB — every row key and cross-view set is keyed by
  endpoint too.

## verified

* vitest 110/110 (8 new multi-project tests: context migration/scope,
  aggregated pending + routed approve, shell badges/switcher), tsc clean.
* live e2e: two real `vouch serve` endpoints + vite + playwright — aggregated
  queue with badges, approve routed to the owning KB (proposal moved to
  decided/, durable claim written, audit hash chain extended; other project
  untouched), vouch's self-approval guard surfaced per-project, scope
  switcher narrows everything live.
* found-by-e2e fix: StrictMode double-mount left every project's capabilities
  null forever (validation results discarded by effect cleanup but endpoints
  stayed marked as checked). cleanup now un-marks discarded validations.

## out of scope

* cross-project chat synthesis (chat asks one project's KB)
* a persistent shared registry of KBs (deployment config stays local)
* server-side aggregation in vouch's own review-ui (rejected direction)
