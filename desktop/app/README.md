# vouch desktop

Native shell for the [vouch review console](https://github.com/vouchdev/vouch) with a **multi-KB picker** ([#207](https://github.com/vouchdev/vouch/issues/207)).

The desktop app wraps the existing Python `vouch review-ui` server in a Tauri window. One window hosts one knowledge base at a time; switching KBs restarts the sidecar against the new project root.

## Features

| Menu item | Behaviour |
|-----------|-----------|
| **File → Open KB…** | Folder picker; folder must contain `.vouch/` or you get an inline error with **create KB here** |
| **File → Recent KBs** | Last five opened KBs, persisted in `~/.config/vouch-desktop/state.json` |
| **File → New KB…** | Pick a folder, run `vouch init`, open the new KB |

The window title updates to `vouch · <project-name>`. The review UI masthead also shows the active KB label.

## Prerequisites

- [Node.js](https://nodejs.org/) 20+
- [Rust](https://rustup.rs/) stable (for Tauri)
- vouch installed with the `[web]` extra: `pip install -e '.[web]'`

Ensure `vouch` is on your `PATH` (or configure the Tauri sidecar — see `src-tauri/sidecars/vouch.json`).

## Development

```bash
cd desktop/app
npm install
npm run tauri:dev
```

`tauri dev` starts the Vite shell on port 1420 for welcome/error screens, then navigates the webview to `http://127.0.0.1:7780/` once a KB is selected.

## Build

```bash
cd desktop/app
npm run tauri:build
```

Artifacts land under `src-tauri/target/release/bundle/`.

## State file

```json
{
  "version": 1,
  "last_kb": "/abs/path/to/project",
  "recent_kbs": [
    { "path": "/abs/path/a", "label": "a", "opened_at": "2026-06-22T12:00:00+00:00" }
  ]
}
```

Path follows [XDG Base Directory](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html) conventions (`%APPDATA%/vouch-desktop` on Windows).

## Python CLI helpers

The shell can invoke these for scripting and tests:

```bash
vouch desktop config-dir
vouch desktop state-show
vouch desktop state-touch /path/to/project --label my-app
vouch desktop kb-check /path/to/project
vouch desktop kb-init /path/to/new-project
vouch init --path /path --json
```

## Architecture

```
┌─────────────────────┐
│  Tauri window       │
│  File menu / title  │
└─────────┬───────────┘
          │ spawn / kill
          ▼
┌─────────────────────┐
│  vouch review-ui    │  ← same review gate as CLI/MCP
│  127.0.0.1:7780     │
└─────────┬───────────┘
          │
          ▼
     <project>/.vouch/
```

Shared logic also lives in `src/vouch/desktop/` (state persistence, KB validation, sidecar spawn helpers) and is covered by `tests/test_desktop.py`.

## Out of scope (v1)

- Side-by-side multi-KB views in one window
- Cross-KB search / federation
