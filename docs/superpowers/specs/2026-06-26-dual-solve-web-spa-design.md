# dual-solve web SPA — design

> status: draft for review. captures the design agreed during brainstorming on
> 2026-06-26. next step after sign-off: an implementation plan via writing-plans,
> then subagent-driven-development.

## goal

a single-page app, shipped inside vouch's existing review-ui, that takes a
github issue link, runs `vouch dual-solve` against the repo the server lives
in, streams progress live, shows both engines' file changes side by side, and
lets a human pick the winner — keeping the chosen branch and proposing its
rationale into the kb through the existing review gate.

## decisions (locked)

- **home:** a shipped vouch feature inside `src/vouch/web` (the FastAPI
  review-ui), not a standalone prototype. full CI gate; review-gate invariant
  preserved.
- **target repo:** the repo the server is launched in (fixed). the SPA sends
  only the issue link; dual-solve runs against the server's own git root. this
  mirrors the CLI's "you're standing in the repo" model — no on-demand cloning.
- **frontend:** Vue 3, **buildless** — loaded via a pinned, vendored
  `vue.esm-browser.prod.js`; no npm/bundler/build step enters the Python repo.
  FastAPI serves it as static files exactly like the current review-ui.
- **review surface:** both candidates' diffs rendered side by side; the human
  clicks a winner.
- **v1 pick semantics:** mirror the CLI — keep the chosen branch *and* propose
  the rationale (decision + optional root-cause + fix claims) into `proposed/`.
- **governance:** this adds an HTTP surface that executes code, so it requires
  an accepted VEP before merge.

## architecture

a new `/dual-solve` view in the existing app (`build_app` in
`src/vouch/web/server.py`). a registration function adds the routes and owns an
in-process job object. the blocking work (`dual_solve.prepare` / `finalize`,
both sync + subprocess-heavy) runs in `run_in_threadpool` so the event loop
stays free. phase progress (the `on_progress` callback already in `prepare`)
is bridged to the existing `_Hub` websocket. one job at a time per server.

```
browser (Vue SPA)  ──HTTP──▶  /dual-solve/run     ─▶ background task
       ▲     ▲                                         └─ run_in_threadpool(prepare)
       │     └──────WS /ws (progress frames)◀────────────  on_progress → hub.broadcast
       │
       └──HTTP── GET /dual-solve/job/{id} (diffs) ── POST /dual-solve/choose ─▶ finalize
```

## files

| kind | path | responsibility |
|---|---|---|
| new | `src/vouch/web/dual_solve_api.py` | `register(app, *, store, hub, auth, guarded, reviewer, git_root, enabled)`; the routes + `DualSolveJob` state |
| new | `src/vouch/web/templates/dual_solve.html` | SPA shell: mounts Vue, includes the static js/css |
| new | `src/vouch/web/static/dual_solve.js` | the Vue 3 app (run form, live progress, side-by-side diff, choose) |
| new | `src/vouch/web/static/dual_solve.css` | layout for the two diff panes |
| new | `src/vouch/web/static/vendor/vue.esm-browser.prod.js` | pinned Vue 3 (vendored; record version + sha in a sibling `VENDOR.md`) |
| modify | `src/vouch/web/server.py` | call `register(...)` (only when enabled); nav link; thread `git_root` + the enable flag |
| modify | `src/vouch/cli.py` (`serve` command) | add `--allow-dual-solve` (default off) |
| new | `proposals/<id>-dual-solve-web.md` (VEP) | the surface-change proposal |
| new | `tests/test_web_dual_solve.py` | TestClient lifecycle + auth + gate + progress |
| modify | `CHANGELOG.md` | `[Unreleased] / ### Added` |

## job state machine

a single `DualSolveJob` held on `app.state`:

```
fields: id, issue_url, claude_effort, codex_effort, status, progress[],
        issue, candidates, engines, proposed_ids, kept_branch, error
status: "running" -> "ready" -> "finalizing" -> "done"
                  \-> "error"            \-> "error"
```

- single-flight: a `POST /run` while a job is `running`/`finalizing` returns
  `409`. once `done`/`error`, a new run replaces it.
- `issue`, `candidates`, `engines` are kept in memory between `prepare` (run)
  and `finalize` (choose) because finalize needs the live `Engine` objects and
  the on-disk worktree paths. acceptable: this is a local, single-operator tool.

## HTTP / WS API

all routes guarded by the existing `require_auth`; all only mounted when
`--allow-dual-solve` is set (otherwise the paths 404).

- `GET /dual-solve` → the SPA shell (HTML).
- `POST /dual-solve/run` `{issue_url, claude_effort?, codex_effort?}`
  → `201 {job_id}`; `409` if a job is active; `400` on an unparseable issue ref.
  launches a background task: `run_in_threadpool(prepare, …, on_progress=bridge)`.
  autonomy is **forced to "edit"** — `full` is never reachable over HTTP.
- `GET /dual-solve/job/{id}` → `{status, progress, issue, candidates:[{engine,
  branch, ok, error, diff}], proposed_ids, kept_branch, error}`.
- `POST /dual-solve/choose` `{job_id, winner: "claude"|"codex"|null, reason}`
  → `run_in_threadpool(finalize, …, record=True)` → `{kept_branch, proposed_ids}`.
  `winner: null` keeps neither (discards both, records nothing).
- `WS /ws` (existing) → progress frames
  `{"type":"dual_solve","job_id":…,"event":"progress"|"ready"|"done"|"error",
   "message":…}`. the existing review frames are unchanged.

**sync→async bridge:** `prepare` runs in a worker thread; `on_progress` is sync
but `hub.broadcast` is a coroutine. capture the running loop at request time and
do `asyncio.run_coroutine_threadsafe(hub.broadcast(frame), loop)` inside the
callback. progress is also appended to `job.progress` so a late websocket
connector (or a poller) can catch up via `GET /job/{id}`.

## frontend (Vue 3, buildless)

one component tree in `dual_solve.js`:

- **RunForm** — issue url input + effort selects + Run button. disabled while a
  job is active.
- **ProgressLog** — subscribes to `/ws`, shows the streaming phase lines and a
  per-engine spinner/elapsed.
- **DiffPanes** — two columns (claude | codex); each parses the unified diff
  into files and hunks and renders added/removed lines with color. a small
  hand-rolled unified-diff parser (no extra dep) is enough; file headers
  collapsible. shows `(failed: …)` for a candidate that errored.
- **ChoiceBar** — Choose claude / Choose codex / Keep neither + a one-line
  reason; posts to `/choose`, then shows the kept branch and links each
  `proposed_id` to the existing review queue (`/`).

state via Vue reactivity; fetch via `fetch()` with the cookie the review-ui
already sets. no router (single view), no build.

## security / trust boundary

the load-bearing concern: this endpoint spawns engines that edit files and run
commands. mitigations, all in v1:

1. **off by default** — routes mount only under `vouch serve --allow-dual-solve`.
   without it, `/dual-solve*` 404s.
2. **localhost-first** — same default bind as the review-ui; same Bearer token
   guards every dual-solve route.
3. **edit-only over HTTP** — `autonomy` is hard-forced to `"edit"`; the
   `full`/bypassPermissions path is unreachable from the web surface.
4. **kb stays gated** — `finalize` only `propose`s; nothing is auto-approved.
5. **no leak** — abandoned jobs are cleaned up (a `done`/`error` job that holds
   worktrees gets `cleanup`'d; a new run cleans a stale prior job first). this
   also closes the CLI `--json` worktree-leak in the web context.

### VEP

a new executing HTTP surface is a surface change; per the repo's rule it needs
an accepted VEP before merge. the VEP documents: the new routes, the
`--allow-dual-solve` gate, the edit-only constraint, and the argument that the
review-gate invariant is preserved (web finalize only proposes). first task in
the implementation plan.

## error handling

- `prepare`/`finalize` raise `ValueError`/`RuntimeError` → job → `error`, message
  surfaced in `job.error` and an `error` ws frame; SPA shows it.
- both engines fail → `ready` with no usable candidate → SPA offers "discard".
- choose with a `job_id` that isn't the active job, or wrong status → `409`.
- a corrupt non-ascii issue title can't poison the kb: `record_to_kb` already
  ascii-coerces claim text at the boundary (existing guard, reused unchanged).

## testing

`tests/test_web_dual_solve.py`, FastAPI `TestClient`, `dual_solve.prepare` and
`finalize` monkeypatched (no real engines / network), mirroring the CLI tests:

- run starts a job → `201 {job_id}`; a second run while active → `409`.
- `GET /job/{id}` returns both candidates' diffs once `ready`.
- choose posts winner+reason → finalize called with `record=True` and the right
  winner → response carries `proposed_ids`; `winner:null` records nothing.
- auth: every dual-solve route is `401` without the token when auth is enabled.
- gate: with `--allow-dual-solve` off, every `/dual-solve*` route is `404`.
- websocket: a `progress` frame arrives on `/ws` during a run (bridge works).
- autonomy is `"edit"` in the `prepare` call regardless of request input.

## out of scope (v1)

- on-demand cloning of arbitrary issue repos (target repo is fixed).
- `full` autonomy over HTTP.
- multi-job concurrency / a job history list.
- a build-step frontend toolchain.
- editing/streaming the engines' own token output (phase-level progress only).
