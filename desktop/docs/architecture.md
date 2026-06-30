# vouch-desktop — Architecture & Build Design

> A cross-platform Electron GUI over the entire vouch command surface. vouch is an **unmodified** Python dependency, spawned as an installed binary. The desktop app talks to it only through its existing transports: **JSONL stdio** (all 54 `kb.*` methods) and **HTTP+WS** (`vouch review-ui` for the live review queue and dual-solve runner). The app never writes durable KB state except through vouch's own `propose → approve` gate.

---

## 0. Design tenets (the load-bearing invariants)

These are non-negotiable and every later section is downstream of them.

1. **vouch is read-only as a dependency.** vouch-desktop lives under `desktop/` in the vouch monorepo, but it still treats the Python package as an external runtime dependency. It never patches, vendors-and-edits, or reaches into vouch's Python source. It shells out to the `vouch` console-script (or `<python> -m vouch`) and speaks the documented wire protocols.
2. **The review gate is sacred.** The only durable writes the app can cause are: (a) `kb.propose_*` / `kb.register_source*` (proposals + ungated evidence intake), and (b) `kb.approve` / `kb.reject` and the lifecycle ops, which are themselves the gate's decision step. The UI must make the gate *visible*, never route around it. There is no "auto-approve on propose" path in the UI.
3. **JSONL is the full surface; HTTP is the live surface.** Every one of the 54 methods is reachable over JSONL. HTTP/WS exists for the two things JSONL can't do well: a *push* feed (review-queue refresh, dual-solve progress) and the *long-running* dual-solve orchestration. Where a capability exists on both, prefer JSONL for request/response and reserve HTTP for push + dual-solve.
4. **No terminal, ever.** A non-technical operator double-clicks an icon. The app locates or bundles vouch, spawns and supervises the child processes, surfaces health, and recovers from crashes — all without the user seeing a shell.
5. **Renderer is sandboxed.** `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`. The renderer never spawns processes or touches the filesystem; it talks to a tiny typed `window.vouch` bridge exposed by the preload. All process I/O lives in main.

---

## 1. Architecture

### 1.1 Process topology

```
┌─────────────────────────────────────────────────────────────────────┐
│ Electron MAIN process (Node, CommonJS or ESM)                         │
│                                                                       │
│  ┌────────────────┐   ┌──────────────────┐   ┌────────────────────┐  │
│  │ VouchLocator   │   │ JsonlClient      │   │ HttpClient         │  │
│  │ (find/bundle   │   │  - spawns        │   │  - spawns          │  │
│  │  the binary)   │   │   `vouch serve   │   │   `vouch review-ui │  │
│  └────────────────┘   │   --transport    │   │   --bind 127...`   │  │
│                       │   jsonl`         │   │  - REST + /ws      │  │
│  ┌────────────────┐   │  - NDJSON over   │   │   client           │  │
│  │ ProcessSuper-  │   │   stdin/stdout   │   └────────────────────┘  │
│  │ visor (health, │   │  - req/resp      │                           │
│  │  restart,      │   │   correlation    │   ┌────────────────────┐  │
│  │  shutdown)     │   └──────────────────┘   │ Tray + Notifier    │  │
│  └────────────────┘                          │ (OS integration)   │  │
│                                                └────────────────────┘  │
│         ▲  IPC (ipcMain.handle / webContents.send)                    │
└─────────┼─────────────────────────────────────────────────────────────┘
          │ contextBridge (preload.ts, sandboxed)
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Electron RENDERER (sandboxed, no node) — the UI                       │
│  window.vouch.call(method, params)  → Promise<result>                 │
│  window.vouch.on(channel, cb)        → live frames (ws, progress)     │
│  Views: read/search · browse · propose · review · sessions ·          │
│         maintenance · export-import · graph · audit · dual-solve      │
└─────────────────────────────────────────────────────────────────────┘
                  │ spawns / pipes / TCP
                  ▼
┌──────────────────────────┐     ┌──────────────────────────────────────┐
│ vouch serve               │     │ vouch review-ui --bind 127.0.0.1:PORT │
│  --transport jsonl        │     │  (only when web features are opened)  │
│  (always running)         │     │  REST + WebSocket /ws                 │
│  stdin/stdout NDJSON       │     │  --no-open-browser --allow-dual-solve │
└──────────────────────────┘     └──────────────────────────────────────┘
          │                                    │
          └───────────► .vouch/ KB ◄───────────┘
              (one KB root, chosen by the user)
```

Two distinct vouch children, both children of the Electron **main** process:

- **The JSONL child** is the workhorse. Spawned once at KB-open, kept alive for the app's lifetime, restarted on crash. Carries all 54 methods.
- **The HTTP child** (`vouch review-ui`) is **lazy** — spawned only when the user opens the Review or Dual-Solve view (or enables "live queue" in settings). It needs the `[web]` extra; if that's missing the app degrades gracefully (review queue falls back to JSONL `kb.list_pending` polling; dual-solve is hidden).

Both children point at the **same KB root** via `VOUCH_KB_PATH=<repo>/.vouch` in the child env (cwd-independent, the recommended discovery pin).

### 1.2 Main-process modules

| Module | Responsibility |
|---|---|
| `main/index.ts` | App entry: single-instance lock, create `BrowserWindow`, wire IPC, build tray, lifecycle hooks. |
| `main/vouch-locator.ts` | Resolve a runnable vouch: configured path → env override → bundled-frozen → repo dev venv → sibling dev checkout → PATH (`vouch`) → `python -m vouch`. Probe `vouch --version` / `kb.capabilities`. |
| `main/jsonl-client.ts` | Spawn `vouch serve --transport jsonl`; line-buffer stdout; correlate requests by envelope `id`; queue + FIFO fallback; surface parse + protocol errors. |
| `main/http-client.ts` | Spawn `vouch review-ui`; wait for `/healthz`; REST helpers (`/api/pending`, `/approve`, `/dual-solve/*`); manage the `/ws` socket and re-fan frames into IPC. |
| `main/supervisor.ts` | Health checks, exponential-backoff restart, crash counters, graceful shutdown (close stdin → SIGINT → SIGKILL). |
| `main/kb-store.ts` | Persist recent KB roots, the chosen vouch binary, ports, tokens (via `safeStorage`), window state, notification prefs. |
| `main/ipc.ts` | Register `ipcMain.handle`/`on` for the channel contract (§1.4); the *only* place renderer calls cross into Node. |
| `main/tray.ts` | Tray/menu-bar icon, badge counts, context menu, notification dispatch. |

### 1.3 Preload (the trust boundary)

`preload/index.ts` runs with `contextIsolation` and exposes exactly one frozen object via `contextBridge.exposeInMainWorld`:

```js
// preload/index.js
const { contextBridge, ipcRenderer } = require("electron");

const ALLOWED_EVENTS = new Set(["vouch:ws", "vouch:progress", "vouch:health", "vouch:proc"]);

contextBridge.exposeInMainWorld("vouch", {
  // request/response over JSONL (the 54 methods)
  call: (method, params) => ipcRenderer.invoke("vouch:call", { method, params }),
  // HTTP-only review-gate actions (form-encoded under the hood, in main)
  http: {
    listPending: (page) => ipcRenderer.invoke("vouch:http:pending", { page }),
    approve: (id, reason) => ipcRenderer.invoke("vouch:http:approve", { id, reason }),
    reject:  (id, reason) => ipcRenderer.invoke("vouch:http:reject",  { id, reason }),
    contradict: (id, against) => ipcRenderer.invoke("vouch:http:contradict", { id, against }),
    dualSolveRun:    (body) => ipcRenderer.invoke("vouch:ds:run", body),
    dualSolveJob:    (jobId) => ipcRenderer.invoke("vouch:ds:job", { jobId }),
    dualSolveChoose: (body) => ipcRenderer.invoke("vouch:ds:choose", body),
  },
  // KB lifecycle / process control
  openKb:  (root) => ipcRenderer.invoke("vouch:openKb", { root }),
  pickKb:  () => ipcRenderer.invoke("vouch:pickKb"),        // native dir dialog
  initKb:  (root) => ipcRenderer.invoke("vouch:initKb", { root }),
  status:  () => ipcRenderer.invoke("vouch:status"),         // proc + health snapshot
  ensureWeb: () => ipcRenderer.invoke("vouch:ensureWeb"),    // lazily spawn review-ui
  capabilities: () => ipcRenderer.invoke("vouch:capabilities"),
  // push channels (main → renderer)
  on: (event, cb) => {
    if (!ALLOWED_EVENTS.has(event)) throw new Error("unknown event: " + event);
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on(event, handler);
    return () => ipcRenderer.removeListener(event, handler);
  },
});
```

No raw `ipcRenderer`, no `require`, no `process` leak into the renderer. The event allow-list prevents the renderer subscribing to arbitrary channels.

### 1.4 The IPC contract

**Request/response channels** (`ipcMain.handle`, renderer awaits a Promise):

| Channel | Payload | Returns | Notes |
|---|---|---|---|
| `vouch:call` | `{method, params}` | `{ok, result}` or `{ok:false, error:{code,message,traceback?}}` | The universal JSONL bridge. Method must be in the known 54; main validates against the catalog before sending. |
| `vouch:openKb` | `{root}` | `{ok, capabilities, status}` | Pin `VOUCH_KB_PATH`, (re)spawn JSONL child, probe `kb.capabilities` + `kb.status`. |
| `vouch:pickKb` | — | `{root\|null}` | `dialog.showOpenDialog({properties:['openDirectory']})`. |
| `vouch:initKb` | `{root}` | `{ok}` | Runs `vouch init` as a one-shot subprocess (the only CLI-subprocess use; see §2.3). |
| `vouch:capabilities` | — | capabilities object | Cached after open. |
| `vouch:status` | — | `{jsonl:{up,pid,restarts}, http:{up,port,auth}, health}` | For the status bar + tray. |
| `vouch:ensureWeb` | — | `{up, port, allowDualSolve}` | Idempotent lazy spawn of `review-ui`. |
| `vouch:http:*` | per §HTTP | JSON / `{ok}` | Form-encoded mutations done in main; renderer never sees 303s. |
| `vouch:ds:*` | dual-solve bodies | JSON | JSON-bodied dual-solve endpoints. |

**Streaming / push channels** (`webContents.send`, renderer subscribes via `vouch.on`):

| Event | Frame | Source |
|---|---|---|
| `vouch:ws` | `{view, action?, proposal_id?, claim_id?, artifact_id?}` | `/ws` `type:"refresh"` frames, re-emitted. Renderer re-pulls the affected view. |
| `vouch:progress` | `{job_id, event, message}` | `/ws` `type:"dual_solve"` frames. Drives the progress log + "ready to judge" notification. |
| `vouch:health` | `{jsonl, http, pendingCount}` | Periodic supervisor poll; drives status bar + tray badge. |
| `vouch:proc` | `{which, state, restarts, error?}` | Process up/down/restarting events for the diagnostics panel. |

**Correlation.** JSONL processing is strictly one-request-one-response, in order. The client still assigns a **monotonic integer `id`** per request and resolves the matching pending Promise when a response with that `id` arrives. A `Map<id, {resolve, reject, timeout}>` holds in-flight calls; FIFO is the fallback if an `id` is ever absent (it never should be, since we always send one). A per-call timeout (default 30 s; configurable for long ops like `index_rebuild`, `reindex_embeddings`, `export`) rejects with a synthetic `timeout` error and tears down the pending entry. The JSONL transport is **synchronous server-side**, so the client also maintains a write queue: requests are written one at a time and the next is sent only after the prior response (or a small pipeline depth) — this prevents head-of-line surprises and keeps the `id`↔response mapping trivially correct even though correlation by `id` would tolerate pipelining.

### 1.5 Process lifecycle

**Start.** On app launch: single-instance lock; restore last KB root from `kb-store`. If a valid root exists and the user opted into auto-open, spawn the JSONL child immediately; otherwise show the KB picker. The HTTP child is **not** started at launch.

**KB-open sequence:**
1. Validate `<root>/.vouch` exists (else offer `initKb`).
2. Resolve the vouch binary (§5.3); if none, show the "install vouch" wizard.
3. Spawn `vouch serve --transport jsonl` with `cwd=<root>`, env `{VOUCH_KB_PATH:<root>/.vouch, VOUCH_AGENT:"vouch-desktop:<os-user>", VOUCH_LOG_FORMAT:"json", VOUCH_LOG_LEVEL:"WARNING"}`, `stdio:["pipe","pipe","pipe"]`.
4. Probe `kb.capabilities` (with a short timeout). Success ⇒ KB is live; cache caps; emit `vouch:health`. Failure / exit code 2 ⇒ "no KB / init needed" UX.

**Health.** Supervisor pings `kb.status` over JSONL every N seconds (cheap, read-only). For the HTTP child, it polls `GET /healthz` (unguarded, returns `pending`, `auth`, `clients`). Results fan out on `vouch:health` and drive the tray badge.

**Restart.** If the JSONL child exits unexpectedly: mark down, reject all in-flight Promises with a `process_down` error, then exponential backoff (250 ms → 4 s, cap 5 attempts/60 s window). On success, re-probe capabilities. If max attempts exceeded, surface a persistent error banner with a "Retry" button and a "view stderr log" link. stderr is **never** parsed as protocol — it is captured to a ring buffer and a rotating log file (`VOUCH_LOG_FILE`) for diagnostics.

**Shutdown.** On `app.before-quit`: stop the supervisor timers; for JSONL, **close child.stdin** (clean EOF-driven exit) then SIGINT after a 2 s grace, SIGKILL after 5 s. For HTTP, send SIGINT (uvicorn catches `KeyboardInterrupt`), then escalate. Persist window + KB state. The single-instance second-launch handler focuses the existing window instead of spawning a duplicate.

### 1.6 Choosing the KB root / working repo

- **First run:** empty-state screen with two actions — **Open existing KB** (native directory picker; we validate `.vouch/` exists by walking the chosen dir, matching `discover_root`) and **Initialize a new KB here** (picker → `vouch init <root>`).
- **Recent KBs:** `kb-store` keeps an MRU list; the tray and a top-bar dropdown switch between them. Switching tears down both children and re-runs the open sequence against the new root.
- **Dual-solve constraint:** the HTTP child with `--allow-dual-solve --dual-solve-sandbox` calls `ds.repo_root` at construction and **fails fast outside a git repo**. So the Dual-Solve view is only enabled when the KB root is itself inside a git working tree (main checks `git rev-parse` via the locator's git probe). Otherwise the view shows a "dual-solve requires the KB to live in a git repo (plus git/gh/docker and the vouch/coder image)" notice.
- We pin discovery with `VOUCH_KB_PATH` rather than relying on cwd, so the choice is launcher-independent and robust.

---

## 2. Backend strategy

### 2.1 Transport decision matrix

| Need | Transport | Why |
|---|---|---|
| Any of the 54 `kb.*` methods, request/response | **JSONL** | Full surface; cheap; synchronous; no port/token. The default for everything. |
| Live review-queue push (a proposal appears / is decided elsewhere) | **HTTP `/ws`** (`refresh` frames) | JSONL has no server-push. WS wakes the UI; we then re-pull via JSONL `kb.list_pending` or HTTP `/api/pending`. |
| Dual-solve orchestration (multi-minute, two engines, progress) | **HTTP** (`/dual-solve/*` + `/ws` `dual_solve` frames) | Only exists on the HTTP surface; long-running + streamed progress. |
| `vouch init` (create a brand-new KB) | **CLI subprocess** | Not a `kb.*` method; one-shot; see §2.3. |

**Rule of thumb:** read/write a method ⇒ JSONL. Need a *push* or *dual-solve* ⇒ HTTP. Bootstrap a KB ⇒ CLI. Never use the HTTP `/approve`,`/reject`,`/contradict` form endpoints *in place of* JSONL for those actions **except** inside the Review view where we want the same `/ws` broadcast to refresh other clients — there we deliberately route approve/reject through HTTP so the audit `approved_by` label and the broadcast match the web console exactly. Outside the live Review view (e.g. an approve triggered from a graph card), JSONL `kb.approve` is fine and equivalent.

### 2.2 The HTTP-vs-JSONL gap (important)

The HTTP surface is **read + decide only**. These are **JSONL/CLI-only** and must be wired to the JSONL client even though they're "review/lifecycle"-flavored:

- `kb.supersede`, `kb.archive`, `kb.confirm`, `kb.expire`, `kb.reject_extracted`
- the entire propose family (`kb.propose_*`, `kb.register_source*`)
- `kb.cite`, `kb.source_verify`
- all read/search/graph/maintenance/session/export-import methods

The HTTP surface exposes only: list pending, view claim/session/source, `/approve`, `/reject`, `/contradict`, `/audit`, and the dual-solve endpoints. So the Review view uses HTTP for *approve/reject/contradict + live refresh*, but every other lifecycle and propose action (and the supersede/archive/confirm/expire/reject_extracted set) goes over **JSONL**.

### 2.3 CLI subprocess — the one sanctioned use

The only thing neither transport offers is **creating a KB**. `vouch init` is a Click command, not a `kb.*` method. `main/vouch-locator.ts` runs it as a detached one-shot (`vouch init <root>`), captures exit code + stderr, and reports success. We do **not** use the CLI for any `kb.*` operation — that would create a parallel data path and is forbidden by tenet 1. (`vouch --version` and a `kb.capabilities` probe are the only other subprocess/JSONL calls used for locating/validating the binary.)

### 2.4 Driving dual-solve

Sequence, exactly mirroring the documented flow, all from main with the HTTP child built `--allow-dual-solve`:

1. **Run:** renderer → `vouch.http.dualSolveRun({issue_url, claude_effort?, codex_effort?})` → main POSTs JSON to `/dual-solve/run`. `201 {job_id}` stored in renderer state. `409` ⇒ a job is already running/finalizing (surface "wait for the current run"). `400` ⇒ bad `issue_url`.
2. **Observe:** main is already subscribed to `/ws`; `type:"dual_solve"` frames for that `job_id` are re-emitted on `vouch:progress`. The renderer appends `progress` lines to a live log. On `ready` (or `done`), it **re-fetches** `GET /dual-solve/job/{job_id}` (frames carry no diffs) to render the two candidate diff panes (`candidates[].diff`, `.ok`, `.error`).
3. **Choose:** renderer → `vouch.http.dualSolveChoose({job_id, winner:'claude'|'codex'|null, reason?})`. `409` if status ≠ `ready`. On success the winner's diff is recorded as a Source and up to 3 **pending** claims are proposed; `{kept_branch, proposed_ids}` returned.
4. **Approve (separate):** the `proposed_ids` land in the normal queue. The user reviews them in the Review view via the gate endpoints. **Dual-solve never auto-approves** — the UI makes that explicit ("3 claims proposed — review them in the queue").

Caveats baked into the UI: single job slot (polling an old `job_id` 404s — we keep only the current one), engines run sequentially (multi-minute; show a spinner + elapsed timer), and the run button is disabled while a job is `running`/`finalizing`. A pre-flight `/healthz` + a sandbox probe (`git`/`gh`/`docker` on PATH and `vouch/coder:latest` available) gates the whole view.

### 2.5 Surfacing the websockets to the renderer

Main owns a single `/ws` connection per HTTP child. It authenticates with `?token=<token>` on the WS URL when auth is enabled (a native client can't read the HttpOnly cookie). On `4401` it surfaces an auth error and retries with the stored token. Frames are normalized and forwarded:

- `type:"hello"` → ignored (logged once).
- `type:"refresh"` → `vouch:ws` (the renderer treats it as a *signal* and re-pulls `/api/pending` or `/audit`). Polling is the source of truth; WS is a wake-up. Main also runs a slow safety poll (e.g. every 20 s) so a missed frame can't strand the queue.
- `type:"dual_solve"` → `vouch:progress`, and main raises the "ready to judge" OS notification on the `ready` event (§4).

The WS client auto-reconnects with backoff; on reconnect the renderer force-re-pulls all live views (a frame may have been missed during the gap).

---

## 3. Renderer UX

### 3.1 Navigation: all 54 methods grouped into ten views

The left rail mirrors the `group` field in the catalog, collapsed to ten task-shaped views:

| View | Methods it surfaces |
|---|---|
| **Dashboard** | `kb.status`, `kb.stats`, `kb.capabilities` (health, counts, pending-by-agent, approval rates, citation coverage). |
| **Search & Ask** | `kb.search`, `kb.context`, `kb.synthesize`. |
| **Browse** | `kb.list_claims`, `kb.list_pages`, `kb.list_entities`, `kb.list_relations`, `kb.list_sources`, and the readers `kb.read_claim`/`read_page`/`read_entity`/`read_relation`, plus `kb.cite`. |
| **Propose** | `kb.propose_claim`, `kb.propose_page`, `kb.propose_entity`, `kb.propose_relation`, `kb.register_source`, `kb.register_source_from_path`. |
| **Review & Lifecycle** | `kb.list_pending`, `kb.approve`, `kb.reject`, `kb.reject_extracted`, `kb.expire`, `kb.supersede`, `kb.contradict`, `kb.archive`, `kb.confirm`. |
| **Sessions** | `kb.session_start`, `kb.session_end`, `kb.volunteer_context`, `kb.crystallize`. |
| **Graph** | `kb.neighbors`, `kb.why`, `kb.trace`, `kb.impact`, `kb.graph_export`, `kb.provenance_rebuild`. |
| **Maintenance** | `kb.index_rebuild`, `kb.lint`, `kb.doctor`, `kb.reindex_embeddings`, `kb.dedup_scan`, `kb.eval_embeddings`, `kb.embeddings_stats`, `kb.source_verify`. |
| **Export / Import** | `kb.export`, `kb.export_check`, `kb.import_check`, `kb.import_apply`. |
| **Audit** | `kb.audit`. |
| **Dual-Solve** | HTTP runner (`/dual-solve/*`). |

A persistent **"Method Console"** (developer drawer, ⌘K) lets a power user invoke *any* method by name through the generic form generator (§3.2) — this is the catch-all that guarantees 100% coverage even before bespoke views exist, and is the first UI milestone after the bridge.

### 3.2 The data-driven form generator (so we never hand-write 54 forms)

We ship the **parameter catalog as JSON** in the renderer (`renderer/data/methods.json`, generated from the verified surface catalog — see §6). Every propose/lifecycle/maintenance form is *generated* from `method.params`, not authored by hand.

**Param-type → control mapping:**

| Catalog `type` | Generated control | Validation / coercion |
|---|---|---|
| `string` | `<input type=text>` (or `<textarea>` when the param is body/text/prose: `text`, `body`, `description`, `note`, `task`, `query`, `reason`, `rationale`) | required-star from `required`; placeholder = `default`. |
| `integer` | `<input type=number step=1>` | coerce to int; default prefilled. |
| `number` | `<input type=number step=any>` (sliders for `confidence`/`min_score`/`threshold`, 0–1) | clamp 0–1 where the description says so. |
| `boolean` | toggle switch | default from catalog. |
| `array<string>` | tag/chip input (type + Enter, or paste comma/newline-separated) | emits `string[]`; empty ⇒ omit. |
| `object` | JSON code editor (Monaco-lite/CodeMirror) with schema-free validation | parse to object; only `metadata` on `propose_page`. |
| **enum (string with documented set)** | `<select>` whose options are parsed from the description's "One of: …" list | see §3.3. |

The generator reads `required`, `default`, and `description` straight from the catalog, renders a label + help text (the `summary` becomes the form header; the `returns` becomes a "what you'll get back" hint), validates required fields client-side, drops empty optionals, coerces types, and calls `window.vouch.call(method, params)`. A **dry-run** checkbox is auto-added to any method whose params include `dry_run` (the propose family + `kb.expire` via `apply`). The result is handed to the matching **result renderer** (§3.4).

This single component covers ~48 of the 54 methods with zero bespoke code. The bespoke exceptions are enumerated in §3.3 and §7.

### 3.3 Enum extraction + the params that need bespoke handling

**Enums are derived, not hard-coded twice.** A build step parses each param `description` for the canonical `"One of: a, b, c"` phrasing and emits an `enum: [...]` array into `methods.json`. Params that get `<select>`s this way: `claim_type`, `entity_type` (both the filter and the required one), `page_type` (plus free-text "other" since a KB may declare extra kinds in config — so it's a *combobox*, select-or-type), `relation`, `source_type`, `backend`, `format`, `on_conflict`, `op`, the `status` filter on `list_claims`. A unit test asserts every documented enum in the catalog has a populated `enum[]` so a future vouch version that adds a value forces a regeneration.

**Params too complex for the pure generic form** (flagged bespoke in §7):

1. **`kb.trace` key divergence.** Over JSONL the param keys are `from`/`to`, but the catalog/MCP names are `from_id`/`to_id`. The generic form would emit `from_id`. **Bespoke shim:** main's JSONL client rewrites `{from_id,to_id}` → `{from,to}` for `kb.trace` before sending. The form stays generic; the adapter is one line in the client.
2. **`metadata` on `kb.propose_page`** (`object`): needs the JSON editor control, not a text box. Handled by the `object` mapping but flagged because malformed JSON must block submit.
3. **ID-reference params** (`evidence[]`, `entities[]`, `claim_ids[]`, `source_ids[]`, `supersedes`, `node_id`, `from`/`to`, `old/new_claim_id`, `claim_a/_b`, `src`/`target`, `against`, `proposal_id`, `*_id` readers): a plain text box works, but UX demands a **typeahead picker** that searches existing artifacts (backed by `kb.search`/`kb.list_*`) and inserts ids. This is a *reusable enhancement* layered on the `string`/`array<string>` controls, keyed by a `refKind` hint in `methods.json` (e.g. evidence→source, entities→entity). Degrades to a raw text box if search is unavailable.
4. **Dual-solve** is wholly bespoke (long-running, two diff panes, choose flow) — not generated.
5. **`kb.eval_embeddings` `queries_path`** and **`kb.export`/`import_*` paths**: file/dir **native pickers** (`dialog.showOpenDialog`/`showSaveDialog` via an IPC helper), not text inputs. Flagged because the path must resolve under the KB root for `register_source_from_path`.

### 3.4 Result renderers

| Result shape | Renderer |
|---|---|
| Claim object | **Claim card**: text, type/status pills, confidence bar, evidence chips (each clickable → `kb.cite` resolve), entities, supersedes/superseded-by/contradicts links, scope, timestamps, `approved_by`. Action buttons (confirm/archive/supersede/contradict) wired to JSONL. |
| Page object | **Page card**: title, rendered markdown body, type/status, linked claim/entity/source chips, frontmatter metadata table. |
| Entity object | **Entity card**: name, type, aliases, description, link to its page, "show neighbors" (→ Graph). |
| Relation object | **Relation row**: `source —[relation]→ target`, confidence, evidence chips. |
| Source object | **Source card**: title, type, locator, byte-size, hash, "claims citing this" (mirrors HTTP `/sources/{id}` reverse index, fetched via list+filter or HTTP). |
| Proposal object | **Proposal card** (review queue item): kind, proposed_by, session, proposed_at, payload preview, rationale, Approve/Reject/(Contradict) controls. |
| Search hits | **Result list**: per-hit kind icon, id, snippet (highlighted), score bar, backend tag; click → opens the matching card. Shows the *actual* backend used + viewer scope. |
| ContextPack | **Context pane**: ranked items with score/backend/freshness/citations, a quality panel (`ok`, min-items, require-citations), warnings banner; "copy as prompt" button. |
| Synthesis | **Answer pane**: prose with inline `[claim_id]` chips that resolve on hover/click, a gaps block, a `synthesis_confidence` grade badge. |
| Graph (neighbors/why/trace/impact) | **Graph canvas**: force-directed (Cytoscape.js) for neighbors/impact/why trees; `trace` renders the shortest path as an ordered chain; node click → card. `impact` shows a "breakage" side panel + a blocking flag. |
| `graph_export` dot/mermaid | **Code+preview pane**: raw text (copyable) + a rendered preview (mermaid live; dot via viz.js). |
| Audit events | **Timeline**: newest-first rows (event, actor, object_ids, at, reason), filter chips by event type; object_ids link to cards. |
| Lint/doctor findings | **Findings table**: severity-colored rows (code, message, object_ids→links), counts header, ok badge. |
| Stats/status | **Dashboard cards**: counts per kind, pending-by-agent bar, approval-rate + citation-coverage gauges over the window. |
| dedup_scan / source_verify / embeddings_stats / eval | **Plain tables** keyed off the returned array/object. |
| Export/import results | **Summary cards**: bundle_id, file counts, new/conflict/identical buckets, issues list; import has an `on_conflict` selector before apply. |
| Generic / unknown | **JSON tree viewer** (collapsible), always available as a fallback so no result is ever unrenderable. The `_meta.vouch_trust` sidebar is shown as a small "local / jsonl" badge, never as primary data. |

Dual-solve gets its own **two-pane diff view** (unified or split, syntax-highlighted), a progress log, and a Choose bar (claude / codex / neither + reason).

---

## 4. Tray / menu-bar companion + notifications + no-terminal launch

### 4.1 Tray / menu-bar

A persistent `Tray` (macOS menu-bar, Windows system-tray, Linux AppIndicator) is the always-available companion:

- **Badge / title:** pending-proposal count (from `vouch:health` → `/healthz.pending` or JSONL `kb.status`). On macOS, also set `app.dock.setBadge` / on Windows the overlay icon.
- **Context menu:** current KB name + switcher (MRU), "Open review queue", "Run dual-solve…", JSONL/HTTP up/down indicators, "Restart vouch", "Open logs folder", "Quit".
- **Click:** focus/raise the main window (or open the Review view directly).
- **Close-to-tray** option so the app keeps supervising vouch and pushing notifications while the window is hidden.

### 4.2 Native OS notifications

Using Electron `Notification` (APNs/Windows toast/libnotify under the hood):

| Trigger | Source | Notification |
|---|---|---|
| **Dual-solve ready to judge** | `vouch:progress` `event:"ready"` | "Dual-solve ready — review the two candidates." Click → focus + open Dual-Solve view for that job. |
| **Dual-solve error** | `event:"error"` | "Dual-solve failed: <message>." Click → open job detail. |
| **New pending proposal(s)** | `vouch:ws refresh view:queue` and/or a health-poll delta in `pending` count | "N new proposal(s) awaiting review." Click → Review view. Debounced (coalesce bursts within ~5 s). |
| **Process down / restart failed** | `vouch:proc` | "vouch stopped responding — restarting / action needed." |

Notifications are opt-in per category (stored in `kb-store`), and we only fire the "new pending" toast when the count *increases* while the window is unfocused (no spam while actively reviewing).

### 4.3 No-terminal launch

The operator never sees a shell:

- The app **owns** the vouch process lifecycle (spawn/health/restart/shutdown, §1.5). All output goes to a captured ring buffer + rotating log file, surfaced via an in-app "Logs" panel and an "Open logs folder" tray item — never a terminal.
- **First-run wizard** if vouch isn't found: a card explains the options and offers (a) "Use the bundled vouch" (if we shipped the frozen build, §5.3), (b) "I have vouch installed — locate it" (file picker, validated by `vouch --version`), or (c) "Install with pipx" (a guided, monitored `pipx install 'vouch[web]'` run with live output, only if Python/pipx is detected). The resolved path is persisted.
- Errors are **human**: "Couldn't find a knowledge base here. Initialize one?" rather than `KBNotFoundError` / `exit code 2`.

---

## 5. Tech choices, build, packaging, locating vouch

### 5.1 Renderer build: electron-vite + React + TypeScript (realized)

The recommended Vite setup from the original design is now fully realized. The
app is built with **electron-vite 2** driving three typed targets from a single
`electron.vite.config.ts`:

- **Main → CJS** (`out/main/index.cjs`): TypeScript compiled to CommonJS so
  Electron's main process can load it normally (root `package.json` is
  `"type":"module"`, so CJS entry files are emitted as `.cjs`).
- **Preload → CJS** (`out/preload/index.cjs`): same rationale; the contextBridge
  script must be CJS.
- **Renderer → React/ESM** (`out/renderer/`): React 18 + TypeScript, bundled by
  Vite with HMR in dev (`ELECTRON_RENDERER_URL`) and `loadFile` in production.

Why React over Preact/vanilla-TS: the form generator and result renderers
accumulate enough component state that React's `useReducer`/`useRef`/
`useImperativeHandle` primitives are directly useful; bundle size is negligible
for a local single-window Electron app. No SSR, no router, no heavy framework.

`electron-vite` builds all three targets with one config, runs `vite` dev server
HMR for the renderer in dev, and satisfies the CJS-main / ESM-renderer split.
The dependency list stays lean: only `react` and `react-dom` are runtime deps;
everything else (electron-vite, vite, typescript, vitest, tsx, @testing-library/*,
@types/*) is a devDependency.

### 5.2 Packaging: electron-builder

`electron-builder` → **dmg** (macOS, signed + notarized when certs are present), **nsis exe** (Windows), **AppImage** (+ optionally deb) for Linux. Config in `electron-builder.yml`:

- `extraResources` carries the bundled frozen vouch (§5.3) under `resources/vouch/` when that build flavor is selected.
- Per-OS code signing / notarization driven by CI secrets; unsigned dev builds for local testing.
- `asar` enabled for app code; the Python bundle stays unpacked (`asarUnpack`) so it's executable.
- Auto-update (electron-updater) is **out of scope for v1** but the layout reserves room for it.

### 5.3 Locating / bundling vouch for a non-technical operator

A **layered resolver** in `vouch-locator.ts`, first hit wins, all validated by spawning `vouch --version` then a `kb.capabilities` JSONL probe:

1. **User-configured path** (from the first-run wizard / settings) — explicit override.
2. **Bundled frozen vouch** — `extraResources/vouch/` produced by **PyInstaller** (`vouch[web]` frozen into a self-contained binary). This is the **default shipped flavor** so a non-technical user needs *zero* Python. Risk: the `[web]` extra (FastAPI/uvicorn/websockets/jinja2/python-multipart) and any embeddings extras must be frozen too if we want dual-solve/review-ui offline; embeddings is heavy, so v1 freezes the `[web]` stack but treats embeddings methods as "available only if your vouch has them" (graceful capability-gating off `kb.capabilities.retrieval`).
3. **PATH `vouch`** — for users who already `pipx install`ed it.
4. **`python -m vouch`** via a detected interpreter — last resort for dev machines.

> **Distribution-name caveat to verify before shipping the installer wizard:** in-code ImportError strings reference the PyPI name `vouch-kb[web]`, while `pyproject` declares the distribution name `vouch`. The pipx-install path in the wizard must use the *actually published* name. We gate the "install with pipx" button behind a resolved name check (`pip index`/`pip download --no-deps` dry probe) and fall back to showing the user the exact command rather than guessing wrong.

Capability-gating: after open we read `kb.capabilities` and **hide or disable** views whose backends aren't present (e.g. embeddings methods — `reindex_embeddings`, `dedup_scan`, `eval_embeddings`, `embeddings_stats` — are disabled with a tooltip if `retrieval` lacks `embedding`; Dual-Solve is hidden if review-ui/`[web]` is unavailable or the KB isn't in a git repo).

---

## 6. File tree (as built)

The tree below reflects the actual `src/` layout produced by the React +
TypeScript + electron-vite reshape. The recommended Vite setup from §5.1 is now
fully realized; file paths match what is in the repository.

```
desktop/
├── package.json                 # "type":"module"; scripts: dev/build/test/typecheck/gen:methods/dist
├── electron.vite.config.ts      # three-target build: main(CJS) / preload(CJS) / renderer(React/ESM)
├── electron-builder.yml         # dmg / nsis / AppImage targets, extraResources
├── tsconfig.json                # solution file (references node + web + scripts projects)
├── tsconfig.node.json           # main + preload + shared → CommonJS
├── tsconfig.scripts.json        # scripts/ + shared → ESNext/Bundler (gen-methods)
├── tsconfig.web.json            # renderer/src + shared → ESNext/Bundler, jsx:react-jsx
├── vitest.config.ts             # environment: jsdom; include: test/**/*.test.{ts,tsx}
├── README.md
├── CHANGELOG.md
├── LICENSE
│
├── build/                       # icons, entitlements.mac.plist, dmg background
│
├── resources/                   # shipped, unpacked from asar
│   └── vouch/                   # PyInstaller-frozen vouch[web] (optional flavor)
│
├── scripts/
│   └── gen-methods.ts           # enrich(catalog) → src/shared/methods.gen.ts + MethodName union
│
├── src/
│   ├── catalog/
│   │   └── methods.json         # verified source catalog (hand-maintained; do not delete)
│   │
│   ├── main/                    # all TypeScript, compiled to CJS by electron-vite
│   │   ├── index.ts             # app entry, BrowserWindow, single-instance lock, lifecycle
│   │   ├── types.ts             # AppCtx interface (shared across main modules)
│   │   ├── ipc.ts               # ipcMain.handle/on contract (§1.4)
│   │   ├── vouch-locator.ts     # resolve/validate/init vouch; git probe; normalizeKbRoot
│   │   ├── jsonl-client.ts      # spawn jsonl child, NDJSON framing, id correlation
│   │   ├── http-client.ts       # spawn review-ui, REST + /ws, frame normalization
│   │   ├── supervisor.ts        # health, restart, shutdown
│   │   ├── kb-store.ts          # MRU roots, binary path, tokens (safeStorage), prefs
│   │   └── tray.ts              # tray, badge, context menu, Notifier
│   │
│   ├── preload/
│   │   └── index.ts             # contextBridge exposeInMainWorld('vouch', …) → VouchApi
│   │
│   ├── renderer/
│   │   ├── index.html           # Vite entry; CSP meta tag for dev/prod
│   │   └── src/
│   │       ├── main.tsx         # createRoot → <App />; imports app.css
│   │       ├── App.tsx          # VouchProvider + shell (Rail/Topbar/View/StatusBar/Drawer)
│   │       ├── app.css          # dark theme (verbatim from original; all class names preserved)
│   │       ├── global.d.ts      # declare global { interface Window { vouch: VouchApi } }
│   │       ├── lib/
│   │       │   ├── client.ts         # unwrap<T>(Envelope<T>) wrappers over window.vouch
│   │       │   ├── format.ts         # timeAgo, truncate
│   │       │   ├── VouchContext.tsx  # useReducer state (root/caps/view/pending) + VouchProvider
│   │       │   ├── useVouchEvents.ts # useEffect push-event subscriptions (vouch:kb/health/proc/tray)
│   │       │   └── useOnOpen.tsx     # onOpen/openNeighbors click-through logic
│   │       ├── components/
│   │       │   ├── Rail.tsx          # left-rail nav (VIEWS array, capability-dimming)
│   │       │   ├── Topbar.tsx        # KB id + cap pills
│   │       │   ├── StatusBar.tsx     # proc dot, pending badge, diagnostics link
│   │       │   ├── EmptyState.tsx    # open/init KB card + recents list
│   │       │   ├── Drawer.tsx        # controlled slide-in drawer
│   │       │   ├── Diff.tsx          # unified-diff renderer (dual-solve)
│   │       │   ├── MethodCard.tsx    # form + run + result for one method
│   │       │   ├── MethodForm.tsx    # forwardRef; collect() parity with original form-gen
│   │       │   ├── Placeholder.tsx   # stub view-body used before view registry is wired
│   │       │   ├── controls/         # Text, Textarea, NumberInput, Slider, Toggle,
│   │       │   │                     #   Select, Combobox, Tags, JsonEditor, Ref, FileControl
│   │       │   └── results/
│   │       │       ├── atoms.tsx     # Pill, IdLink, ConfidenceBar, MetaRow, LinkChips
│   │       │       ├── JsonTree.tsx  # recursive collapsible fallback viewer
│   │       │       ├── Cards.tsx     # ClaimCard, PageCard, EntityCard, RelationRow,
│   │       │       │                 #   SourceCard, ProposalCard, ProposeResult
│   │       │       ├── Renderers.tsx # SearchResults, ContextPack, Synthesis, AuditTimeline,
│   │       │       │                 #   Findings, GraphCode, and shape-dispatch helpers
│   │       │       └── ResultView.tsx# top-level result dispatcher used by MethodCard
│   │       └── views/
│   │           ├── registry.tsx  # view-id → React.FC map; routes App's ViewRouter
│   │           ├── blurbs.ts     # viewBlurb descriptions
│   │           ├── GenericView.tsx   # header + MethodCard per view's methods
│   │           ├── Dashboard.tsx     # caps/status/stats cards + quick-actions
│   │           ├── Review.tsx        # pending queue (ProposalCard) + lifecycle methods
│   │           └── DualSolve.tsx     # HTTP runner: run → progress → diff panes → choose
│   │
│   └── shared/                  # imported by main, preload, AND renderer
│       ├── ipc.ts               # Envelope<T>, VouchApi, all channel payload types
│       ├── methods.types.ts     # Method, Param, ControlType, RefKind, FileMode
│       └── methods.gen.ts       # GENERATED — do not edit; run npm run gen:methods
│
└── test/
    ├── catalog.test.ts          # 6 tests: 54 methods, views, enums, trace keys, ref/file params
    ├── gen-methods.test.ts      # 4 tests: enrich() pure-function contract
    ├── vouch-locator.test.ts    # locator order, sandbox detection, normalizeKbRoot
    ├── http-client.test.ts      # 2 tests: sandbox flag, custom image
    ├── jsonl-client.test.ts     # 7 tests: JSONL framing and correlation
    ├── method-form.test.tsx     # 7 tests: collect() parity (RTL)
    ├── controls.test.tsx        # 47 tests: every control type (RTL)
    └── method-card.test.tsx     # 9 tests: MethodCard run/error/spinner (RTL)
```

---

## 7. Completeness table — every one of the 54 methods → view + control

`G` = pure generic form. `G+` = generic form plus a reusable enhancement (enum-select / ref-typeahead / file-picker). `B` = bespoke control. Transport: **J** = JSONL, **H** = HTTP, **C** = CLI.

| # | Method | View | Control | Transport | Bespoke? |
|---|---|---|---|---|---|
| 1 | `kb.capabilities` | Dashboard | auto-run on open; "refresh" button | J | G (no params) |
| 2 | `kb.status` | Dashboard | auto-run; health poll | J | G |
| 3 | `kb.stats` | Dashboard | `days` number input | J | G |
| 4 | `kb.search` | Search & Ask | query + limit + **backend select** + min_score slider + project/agent | J | G+ (enum) |
| 5 | `kb.neighbors` | Graph | **node_id ref-typeahead** + depth + rel_types tags + max_nodes | J | G+ (ref) |
| 6 | `kb.context` | Search & Ask | full generic form (incl. JSONL-only fail_on_*/graph_rel_types) | J | G+ (ref for graph_rel_types) |
| 7 | `kb.synthesize` | Search & Ask | query + depth + max_chars + llm toggle | J | G |
| 8 | `kb.read_page` | Browse | **page_id ref-typeahead** → Page card | J | G+ (ref) |
| 9 | `kb.read_claim` | Browse | **claim_id ref-typeahead** → Claim card | J | G+ (ref) |
| 10 | `kb.read_entity` | Browse | **entity_id ref-typeahead** → Entity card | J | G+ (ref) |
| 11 | `kb.read_relation` | Browse | **relation_id ref-typeahead** → Relation row | J | G+ (ref) |
| 12 | `kb.list_pages` | Browse | list (no params) → Page cards | J | G |
| 13 | `kb.list_claims` | Browse | **status select** filter → Claim cards | J | G+ (enum) |
| 14 | `kb.list_entities` | Browse | **entity_type select** filter → Entity cards | J | G+ (enum) |
| 15 | `kb.list_relations` | Browse | optional **node_id ref** filter → Relation rows | J | G+ (ref) |
| 16 | `kb.list_sources` | Browse | list → Source cards | J | G |
| 17 | `kb.list_pending` | Review | list → Proposal cards (also live via WS) | J / H | G |
| 18 | `kb.register_source` | Propose | content textarea + title + url + **source_type select** + media_type | J | G+ (enum) |
| 19 | `kb.register_source_from_path` | Propose | **path file-picker (under KB root)** + title + url + source_type select | J | **B** (file picker + root check) |
| 20 | `kb.propose_claim` | Propose | text + **evidence ref-typeahead[]** + claim_type select + confidence slider + entities ref[] + rationale + tags + slug_hint + session_id + dry_run | J | G+ (ref + enum) |
| 21 | `kb.propose_page` | Propose | title + body(markdown) + page_type **combobox** + claim/entity/source ref[] + **metadata JSON editor** + rationale + slug_hint + session + dry_run | J | **B** (JSON metadata + combobox) |
| 22 | `kb.propose_entity` | Propose | name + **entity_type select** + aliases[] + description + rationale + slug_hint + session + dry_run | J | G+ (enum) |
| 23 | `kb.propose_relation` | Propose | **src ref** + **relation select** + **target ref** + confidence slider + evidence ref[] + rationale + session + dry_run | J | G+ (ref + enum) |
| 24 | `kb.approve` | Review | Approve button on Proposal card (optional reason) | H (live) / J | G |
| 25 | `kb.reject` | Review | Reject button (**reason required**) | H (live) / J | G |
| 26 | `kb.reject_extracted` | Review & Lifecycle | optional page_id ref + reason; "clear all auto-extracted" action | J | G+ (ref) |
| 27 | `kb.expire` | Review & Lifecycle | apply toggle (dry-run default) + days; shows would_expire vs expired | J | G |
| 28 | `kb.supersede` | Review & Lifecycle | **old_claim_id ref** + **new_claim_id ref** (self-supersede blocked client-side) | J | G+ (ref) |
| 29 | `kb.contradict` | Review & Lifecycle | **claim_a ref** + **claim_b ref** (also surfaced on Claim card; HTTP variant in live Review) | J (+ H in live Review) | G+ (ref) |
| 30 | `kb.archive` | Review & Lifecycle | **claim_id ref** (also Claim-card button) | J | G+ (ref) |
| 31 | `kb.confirm` | Review & Lifecycle | **claim_id ref** (also Claim-card button) | J | G+ (ref) |
| 32 | `kb.cite` | Browse | resolved on Claim card (evidence chips) + standalone claim_id ref | J | G+ (ref) |
| 33 | `kb.source_verify` | Maintenance | run button (no params) → verification table | J | G |
| 34 | `kb.session_start` | Sessions | task + note; starts the "active session" banner | J | G |
| 35 | `kb.session_end` | Sessions | **session_id ref** + note | J | G+ (ref) |
| 36 | `kb.volunteer_context` | Sessions | **session_id ref** + clear toggle → volunteers list | J | G+ (ref) |
| 37 | `kb.crystallize` | Sessions | **session_id ref** + write_summary_page toggle → approved/failures summary | J | G+ (ref) |
| 38 | `kb.index_rebuild` | Maintenance | run button (long-op timeout) → rebuild summary | J | G |
| 39 | `kb.lint` | Maintenance | stale_days number → Findings table | J | G |
| 40 | `kb.doctor` | Maintenance | run button → Findings table | J | G |
| 41 | `kb.export` | Export/Import | **out_path save-picker** → bundle summary | J | **B** (save picker) |
| 42 | `kb.export_check` | Export/Import | **bundle_path open-picker** → integrity report | J | **B** (open picker) |
| 43 | `kb.import_check` | Export/Import | **bundle_path open-picker** → new/conflict/identical buckets | J | **B** (open picker) |
| 44 | `kb.import_apply` | Export/Import | **bundle_path open-picker** + **on_conflict select** → merge summary | J | **B** (picker + enum) |
| 45 | `kb.audit` | Audit | tail number + project/agent → Timeline | J | G |
| 46 | `kb.reindex_embeddings` | Maintenance | backfill/force toggles + model (capability-gated) | J | G (gated) |
| 47 | `kb.dedup_scan` | Maintenance | threshold slider + dry_run → candidate-pairs table (gated) | J | G (gated) |
| 48 | `kb.eval_embeddings` | Maintenance | **queries_path file-picker** + k → metrics (gated) | J | **B** (file picker) |
| 49 | `kb.embeddings_stats` | Maintenance | run button → stats (gated) | J | G |
| 50 | `kb.why` | Graph | **claim_id ref** + depth → provenance tree canvas | J | G+ (ref) |
| 51 | `kb.trace` | Graph | **from/to ref** → path chain. **trace key shim** (`from_id`→`from`) in client | J | **B** (key remap) |
| 52 | `kb.impact` | Graph | **claim_id ref** + depth + **op select** → dependents tree + breakage panel | J | G+ (ref + enum) |
| 53 | `kb.graph_export` | Graph | optional session ref + **format select** → code+preview pane | J | G+ (enum) |
| 54 | `kb.provenance_rebuild` | Graph/Maintenance | run button (no params) → edge count | J | G |

**Coverage check:** all 54 mapped. Pure-generic (`G`): 14. Generic+enhancement (`G+`): 29. Bespoke (`B`): 8 — `register_source_from_path` (19), `propose_page` (21), `export` (41), `export_check` (42), `import_check` (43), `import_apply` (44), `eval_embeddings` (48), `trace` (51). Plus the **whole Dual-Solve view** is bespoke (HTTP, not a `kb.*` method) and the **live Review approve/reject/contradict** path is bespoke-routed over HTTP for the broadcast. A catalog test (`test/catalog/`) asserts this table stays exhaustive and that every enum param has a derived `enum[]`.

---

## 8. Phased build plan (riskiest unknowns first)

### Risk register (surface and de-risk these before they block a phase)

- **R1 — Bundling/freezing vouch (highest).** Does PyInstaller cleanly freeze `vouch[web]` (FastAPI/uvicorn/websockets/jinja2/multipart) cross-platform? Embeddings extras are heavy and may not freeze well. **De-risk in Phase 0** with a spike on all three OSes; fall back to "detect installed vouch + guided pipx" if freezing the web stack proves brittle.
- **R2 — Distribution name (`vouch` vs `vouch-kb[web]`).** Code error strings disagree with `pyproject`. The pipx wizard must use the published name. **Verify before any installer ships** (probe `pip index`).
- **R3 — JSONL long-ops + timeouts.** `index_rebuild`, `reindex_embeddings`, `export`, `import_apply` can run long; the generic 30 s timeout is wrong for them. Need per-method timeout overrides and a "still working" UI.
- **R4 — review-ui `[web]` extra availability + dual-solve preconditions.** If `[web]` isn't installed the HTTP child won't start; if not in a git repo (or git/gh/docker/image missing) dual-solve fails at construction. Capability + PATH probing must gate the UI cleanly.
- **R5 — WS auth from a native client.** No HttpOnly cookie; must use `?token=`; `4401` handling and reconnect. Lower risk but easy to get wrong.

### Phase 0 — Spikes (de-risk R1, R2, R4)
PyInstaller freeze of `vouch[web]` on macOS/Win/Linux; confirm `vouch serve --transport jsonl` and `vouch review-ui` run from the frozen binary; confirm the published pip name. Output: a go/no-go on bundling vs detect-only.

### Phase 1 — Scaffold
`electron-vite` + electron-builder skeleton; single window; sandboxed preload with a stub `window.vouch`; `gen-methods.ts` producing `methods.json` + types + the catalog test; CI building all three targets and running unit tests.

### Phase 2 — Process bridge (the spine)
`vouch-locator` (resolve + validate + `vouch init`), `jsonl-client` (spawn, NDJSON framing, id-correlation, write queue, per-method timeouts incl. the trace key shim), `supervisor` (health/restart/shutdown), `kb-store` (MRU + safeStorage), the full `vouch:call` IPC path, and the KB-open flow with the empty-state picker. **Definition of done:** can open a KB and round-trip `kb.capabilities` + `kb.status` + a `kb.search` from a dev renderer.

### Phase 3 — Generic Method Console (instant 100% coverage)
The data-driven form generator + all control types (text/number/slider/toggle/tags/json-editor/enum-select/file-picker) + the JSON-tree fallback renderer + the ⌘K console that can invoke **any** of the 54 methods. This alone makes every method usable before bespoke views exist. Wire the ref-typeahead (search-backed id pickers).

### Phase 4 — Bespoke views & result renderers
Dashboard, Search & Ask (context/synthesis panes), Browse (cards + `kb.cite` chips), Propose (rich forms incl. `propose_page` metadata + combobox), Review & Lifecycle (proposal cards + the JSONL lifecycle ops), Sessions, Graph (Cytoscape + why/trace/impact/graph_export), Maintenance (findings tables + capability-gated embeddings), Export/Import (native pickers), Audit (timeline). Result renderers per §3.4.

### Phase 5 — HTTP/WS live features
Lazy-spawn `review-ui`; `/healthz` gating; `/api/pending` polling + `/ws` refresh wake-ups; route live approve/reject/contradict over HTTP; then the full **Dual-Solve** view (run → progress log → diff panes → choose → "proposed, go review"). WS auth via `?token=`, reconnect, missed-frame re-pull.

### Phase 6 — Tray, notifications, no-terminal polish
Tray + badge + KB switcher + close-to-tray; OS notifications (dual-solve ready/error, new-pending delta, process-down) with opt-in prefs and debounce; first-run install/locate wizard; human error messaging; logs panel.

### Phase 7 — Packaging & release
electron-builder dmg/exe/AppImage; bundle the frozen vouch as `extraResources` (or ship detect-only if R1 went no-go); macOS signing + notarization; Windows signing; smoke-test the packaged artifact on each OS opening a real `.vouch/` KB; Playwright-electron e2e (open KB, propose→approve round-trip, dual-solve against a mock issue).

Each phase ends green on `make`-equivalent CI (typecheck + unit + catalog tests); the catalog test guarantees no method silently falls out of coverage as the surface evolves.

---

Design doc written to drive the build. Key load-bearing decisions: JSONL for the full 54-method surface with a lazy HTTP child only for live review + dual-solve; a single data-driven form generator off `methods.json` (generated from the verified catalog, with enum extraction) covering ~46/54 methods so we never hand-write forms; 8 bespoke controls flagged (file/save pickers, `propose_page` metadata JSON, and the `kb.trace` `from_id`→`from` key shim); review gate preserved by never exposing an auto-approve path and routing live approve/reject through HTTP only for the WS broadcast. Riskiest unknown is PyInstaller-freezing `vouch[web]` cross-platform (Phase 0 spike, with detect-installed + guided-pipx fallback), plus the `vouch` vs `vouch-kb[web]` published-name discrepancy that must be verified before the installer wizard ships.

The full markdown document is my response above (sections 0-8). Relevant source surfaces of record live under the monorepo root: `src/vouch/web/server.py`, `src/vouch/dual_solve.py`, `src/vouch/jsonl_server.py`, `src/vouch/proposals.py`, and `src/vouch/lifecycle.py`.
