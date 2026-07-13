# vouch-ui

A chat-first browser console for [vouch](https://github.com/vouchdev/vouch) — the
git-native, review-gated knowledge base for LLM agents. Inspired by the AgentOS
control-plane UX: point it at a running endpoint and talk to your KB.

## What you get

- **Chat** — ask questions; answers are synthesized *only* from approved claims,
  with clickable citation chips, a confidence badge, and explicit gaps. `/search
  <term>` runs a raw index search with highlighted hits. The terminal toggle
  switches to **Claude Code mode**: messages run `claude -p` headless in the
  project workspace (dev server only, via `plugins/claude-bridge.ts`), stream
  progress, and keep session continuity turn to turn. Set `VOUCH_PROJECT_DIR`
  to point the bridge at a different workspace (default: `../vouch`).
- **Review** — captured sessions that still need an LLM summary: one click
  runs `kb.summarize_session` and the result moves on to Pending.
- **Pending** — the propose → approve gate, made visible: pending queue,
  payload inspection, approve / reject (with a required, audited reason).
- **Claims** — every approved claim, with a **Start Here** button that opens
  the chat in Claude Code mode seeded with the claim — resuming the claim's
  originating session when provenance records one.
- **Browse** — claims, pages, entities, relations, with citations and
  provenance ("why does this claim exist?") in a detail drawer.
- **Stats** — artifact counts, review throughput, citation coverage, endpoint
  health and capabilities.

## Quick start

This app lives inside the [vouch](https://github.com/vouchdev/vouch) repo under
`webapp/`. From the repo root, one command runs the backend and this console
together (installs deps on first run, Ctrl-C stops both):

    make console                          # vouch serve + this UI, then open :5173

To run just the console against a vouch you started yourself:

    # 1. serve a knowledge base (any directory with a .vouch/)
    vouch serve --transport http          # binds 127.0.0.1:8731

    # 2. run the console
    npm install
    npm run dev                           # http://localhost:5173

Enter the endpoint (default `http://127.0.0.1:8731`) and an optional bearer
token in the connect dialog. The Vite server proxies requests same-origin
(`/proxy/*` → your endpoint via the `X-Vouch-Target` header), so vouch needs
no CORS configuration and stays unmodified.

## Scripts

    npm run dev        # dev server
    npm run build      # typecheck + production build
    npm run preview    # serve the production build (proxy included)
    npm test           # unit + component tests (vitest)
    npm run e2e        # playwright smoke test (needs `vouch` on PATH)

## Notes

- The UI is capability-aware: views light up based on what `GET /capabilities`
  advertises. A read-only endpoint renders a read-only console.
- vouch's default self-approval guard applies: approving a proposal you
  proposed (as the same agent identity) fails with `forbidden_self_approval`.
