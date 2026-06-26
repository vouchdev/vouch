---
vep: 0006
title: dual-solve web runner
author: plind-junior
status: draft
created: 2026-06-26
landed-in: ""
supersedes: []
superseded-by: ""
---

# VEP-0006: dual-solve web runner

## Summary

Add a browser-based single-page app to the review-ui that runs `vouch
dual-solve` against a GitHub issue link, streams progress live, displays
both engines' proposed changes side by side, and lets a human pick a winner
— proposing the chosen branch's rationale into the knowledge base through
the existing review gate.

## Motivation

The `vouch dual-solve` CLI command today requires terminal access and
manual copy-paste of results. Users running dual-solve on GitHub issues
(the primary use case) need to:

1. SSH into the server or clone the issue repo locally
2. run `vouch dual-solve <issue-url>` and wait for both engines
3. manually review each engine's diff in the terminal
4. pick a winner and run `vouch finalize` to record the outcome

a web surface inside the existing review-ui lowers the friction: open a
URL, paste the issue link, watch progress stream live in side-by-side
diffs, and click to finalize — without ever touching the terminal.

## Proposal

Add four new routes to the review-ui FastAPI app (alongside the existing
`/` review interface):

- `GET /dual-solve` — the SPA shell (HTML). loads vendored Vue 3.
- `POST /dual-solve/run` — launch a job: `{issue_url, claude_effort?, codex_effort?}` → `201 {job_id}`. fails with `409` if a job is already running.
- `GET /dual-solve/job/{id}` — fetch job state: `{status, progress, issue, candidates: [{engine, branch, ok, error, diff}], proposed_ids, kept_branch, error}`.
- `POST /dual-solve/choose` — finalize and record: `{job_id, winner: "claude" | "codex" | null, reason}` → `{kept_branch, proposed_ids}`.
- Reuse existing `WS /ws` websocket for progress frames: `{type: "dual_solve", job_id, event: "progress" | "ready" | "done" | "error", message}`.

routes mount only when `vouch serve --allow-dual-solve` is set (default
off); all require Bearer-token auth via the existing `require_auth`
decorator (inherited from the review-ui).

new on-disk files:

- `src/vouch/web/dual_solve_api.py` — HTTP routes and `DualSolveJob` state machine.
- `src/vouch/web/templates/dual_solve.html` — SPA shell.
- `src/vouch/web/static/dual_solve.js` — Vue 3 app (run form, live progress, diff viewer, choice bar).
- `src/vouch/web/static/dual_solve.css` — layout for side-by-side diffs.
- `src/vouch/web/static/vendor/vue.esm-browser.prod.js` — vendored Vue 3; pinned version + sha recorded in `VENDOR.md`.

modifications:

- `src/vouch/web/server.py` — call `register(...)` to mount routes when enabled; thread `git_root` and the enable flag.
- `src/vouch/cli.py` (`serve` command) — add `--allow-dual-solve` flag (default False).
- `CHANGELOG.md` — document new feature.

## Design

the job state machine:

```
status: "running" -> "ready" -> "finalizing" -> "done"
                  \-> "error"            \-> "error"
```

a single `DualSolveJob` is held on `app.state`. `prepare` (which runs the
engines and generates diffs) executes in `run_in_threadpool` to keep the
event loop free; the `on_progress` callback from `dual_solve.prepare` is
bridged to the existing `_Hub` websocket using
`asyncio.run_coroutine_threadsafe(hub.broadcast(frame), loop)`.

progress is also appended to `job.progress` (in-memory) so late websocket
connectors can poll `GET /job/{id}` and catch up.

`finalize` (which records the winner into the KB) also runs in a thread
pool and calls `dual_solve.finalize(..., record=True)`, which only ever
`propose`s claims — nothing bypasses the review gate. the `finalize` call
preserves the live `Engine` objects and on-disk worktrees needed to record
the outcome.

autonomy is hard-forced to `"edit"` in the `prepare` call regardless of
any request input — the `full`/bypassPermissions path is unreachable over
HTTP.

## Compatibility

- **.vouch/ layout:** unchanged. no migration required.
- **Bundle format / audit-log shape:** unchanged.
- **kb.capabilities:** no changes to the method surface. `capabilities`
  remains read-only.
- **Default behavior:** unchanged. `vouch serve` with no `--allow-dual-solve`
  flag behaves exactly as before; the `/dual-solve*` routes do not mount and
  return 404.
- **Existing review-ui:** the new routes are additive; the existing `/` review
  interface, `/ws` websocket, and auth model are unmodified.

## Security implications

This adds an HTTP surface that spawns engines (which edit files and run
commands), so it requires careful trust-boundary analysis.

1. **off by default.** Routes only mount under `vouch serve --allow-dual-solve`.
   without the flag, `/dual-solve*` 404s. the operator must explicitly opt in.
2. **localhost-first.** same default binding as the review-ui (127.0.0.1:8731).
   same Bearer token guards every dual-solve route. binding a non-loopback address
   requires `--allow-public` + a token (inherited from the HTTP transport spec).
3. **edit-only over HTTP.** autonomy is hard-forced to `"edit"`. the
   `full`/bypassPermissions path is unreachable. the web surface can never
   auto-approve proposals or bypass the review gate.
4. **kb stays gated.** `finalize` only `propose`s claims into `proposed/`.
   nothing is auto-approved. the review-gate invariant (every write goes through
   `proposals.approve()`) is preserved.
5. **job cleanup.** abandoned jobs (a `done` or `error` job holding worktrees)
   are cleaned up by `DualSolveJob.cleanup()`. a new run cleans any stale prior
   job first. this closes the CLI `--json` worktree-leak in the web context.
6. **non-ascii title guard.** corrupt non-ascii issue titles can't corrupt the
   KB: `record_to_kb` already ascii-coerces claim text at the boundary (existing
   guard, reused unchanged).

## Performance implications

not on a hot path for the common case (CLI). the job runs in a thread pool so
the event loop remains free for other clients. one job at a time per server
(acceptable for a local tool).

## Open questions

(none at this time. see Alternatives considered and Out of scope below.)

## Alternatives considered

- **emit diffs as streaming newline-delimited JSON instead of storing in memory.**
  rejected for v1: the `choose` step needs the live Engine objects to finalize
  correctly. keeping them in memory is acceptable for a local, single-operator tool.
- **support multi-job concurrency with a job history list.** rejected for v1:
  single-flight semantics (one job at a time) are simpler and match the CLI's
  blocking model. if demand appears, a follow-up VEP can add this.
- **expose the full autonomy `full`/bypassPermissions path over HTTP with a
  separate auth level.** rejected: the review gate is the load-bearing invariant;
  it should never be optional, even with a separate token. the web surface always
  uses `edit`-only autonomy.
- **support on-demand cloning of arbitrary issue repos.** rejected for v1: the
  SPA is designed to run against the server's own git root (the repo the server
  is launched in). this mirrors the CLI's "you're standing in the repo" model
  and avoids the complexity of arbitrary clones. if demand appears, a follow-up
  VEP can extend this.

## References

- [`docs/superpowers/specs/2026-06-26-dual-solve-web-spa-design.md`](../docs/superpowers/specs/2026-06-26-dual-solve-web-spa-design.md) — full design document
- [`ROADMAP.md`](../ROADMAP.md) — dual-solve feature roadmap
- [VEP-0004: HTTP transport](VEP-0004-http-transport.md) — auth and trust-boundary precedent
