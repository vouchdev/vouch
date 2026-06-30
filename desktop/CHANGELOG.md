# Changelog

All notable changes to vouch-desktop are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] — 2026-06-26

### Changed
- **Reshaped to React + TypeScript + electron-vite.** The renderer is rebuilt as
  React 18 components (`src/renderer/src/`); main and preload are converted from
  plain JS to TypeScript. electron-vite replaces the buildless setup, producing
  three typed targets (main → CJS, preload → CJS, renderer → React/ESM).
- **Typed IPC contract.** `src/shared/ipc.ts` declares the `VouchApi` interface
  and all channel payload/return types; main, preload, and renderer share it as a
  single source of truth. No behavior change — same channels, same semantics.
- **Generated typed catalog.** `scripts/gen-methods.ts` now emits
  `src/shared/methods.gen.ts` (a typed `Method[]` array + a `MethodName` union)
  instead of a plain JSON file. The generator's `enrich()` function is unit-tested.
- All 11 views, the dark theme, the form generator, result renderers, review gate,
  and dual-solve runner are faithful ports — **behavior unchanged**.
- Tests migrated to Vitest; 85 tests covering catalog, gen-methods, vouch-locator,
  http-client, jsonl-client, MethodForm collect parity, controls, and MethodCard.

## [0.1.0] — 2026-06-26

### Added
- Initial Electron desktop app: a GUI over the full vouch `kb.*` command surface
  (all 54 methods), plus a bespoke dual-solve runner.
- **Process bridge.** Main-process `JsonlClient` spawns `vouch serve --transport
  jsonl` and exchanges newline-delimited JSON, correlating responses by `id`,
  with per-method timeouts (long ops get 10 min). A `Supervisor` health-polls
  `kb.status`, restarts the child with backoff, and surfaces process state.
- **Lazy dual-solve.** An `HttpClient` spawns
  `vouch review-ui --allow-dual-solve --dual-solve-sandbox` only when the
  Dual-Solve view is opened, drives `/dual-solve/run|job|choose`, and streams
  phase progress over the review-ui `/ws`. The review queue itself runs over
  JSONL, so the rest of the app needs no `[web]` extra.
- **Data-driven forms.** A single form generator builds typed controls from a
  verified parameter catalog (`scripts/gen-methods.ts` → `src/shared/methods.gen.ts`):
  text/textarea, number/slider, toggle, tag input, JSON editor, enum select and
  combobox (option lists taken from vouch's own model enums), native file/save
  pickers, and a search-backed id typeahead for reference params.
- **Views.** Dashboard, Search & Ask, Browse, Propose, Review & Lifecycle (with a
  pending-queue + approve/reject), Sessions, Graph, Maintenance, Export/Import,
  Audit, and Dual-Solve. A detail drawer opens any claim/page/entity/relation.
- **Capability-gating.** Methods not advertised by the connected vouch are shown
  disabled; views light up automatically when vouch is upgraded.
- **Companion.** Tray icon with pending-count badge + KB switcher, native OS
  notifications (dual-solve ready, new pending proposals, process down), and a
  no-terminal launch flow (open/init a KB, the app supervises vouch).
- **Review gate preserved.** The UI never auto-approves; durable writes only ever
  happen through vouch's own `propose → approve` flow.
- Sandboxed renderer (`contextIsolation`, `sandbox`, no node), a single frozen
  `window.vouch` preload bridge, and a tight Content-Security-Policy.
- electron-builder config for dmg / nsis / AppImage; a layered vouch locator
  (configured path → bundled-frozen → PATH → `python -m vouch`).
- Tests: a catalog-coverage suite (`npm test`) and a JSONL smoke harness against
  a real vouch binary (`npm run smoke <kbRoot>`).

### Not yet covered
- The CLI-only orchestration commands `auto-pr`, `migrate`, `schema`, and
  `sync-check`/`sync-apply` are not yet surfaced in the UI.
- Packaging a bundled, frozen vouch (PyInstaller) for zero-Python installs is
  scaffolded (`scripts/freeze-vouch.sh` slot, `extraResources`) but not built.
