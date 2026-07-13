# vouch-ui — AgentOS-style console for vouch

**Date:** 2026-07-04
**Status:** Approved (all four sections approved in brainstorming session)

## Summary

A chat-first, browser-based control plane for [vouch](https://github.com/vouchdev/vouch)
(the git-native, review-gated knowledge base for LLM agents), modeled on the UX of
https://os.agno.com/: point the UI at a locally running endpoint, then interact with
the knowledge base primarily through a chat surface, with secondary views for the
review gate, knowledge browsing, and stats.

vouch is an **unmodified dependency**. The UI talks exclusively to
`vouch serve --transport http` (default `http://127.0.0.1:8731`), which exposes:

- `POST /rpc` — vouch-native envelope: request `{"id", "method", "params"}`,
  response `{"id", "ok": true, "result"}` or `{"id", "ok": false, "error": {"code", "message"}}`
- `GET /health`, `GET /capabilities` — unauthenticated probes
- Bearer-token auth on `/rpc` via `Authorization: Bearer <token>`

## Decisions made during brainstorming

| Decision | Choice |
|---|---|
| Center of gravity | Chat-first KB console (synthesize with citations); other views secondary |
| Distribution | Standalone SPA; user enters endpoint + token in a connect dialog (agno model) |
| V1 views | Chat, Review queue, Knowledge browser, Stats & health |
| Stack | Vite + React + TypeScript + Tailwind CSS |
| CORS bridge | Custom Vite plugin: same-origin `/proxy/*` forwarded to the endpoint named in an `X-Vouch-Target` header |

## 1. Architecture & connection

Pure client-side SPA in `vouch-ui/`. No server runtime of its own beyond the Vite
dev/preview server, which carries a **custom proxy plugin** (~40 lines):

- Middleware on `/proxy/*` (registered for `npm run dev` **and** `vite preview`).
- Reads the target base URL from the `X-Vouch-Target` request header; forwards the
  request (method, body, `Authorization` header) to `<target><path minus /proxy>`;
  streams the response back.
- Only forwards to `http:`/`https:` targets; anything else is rejected with 400.
- Security: `X-Vouch-Target` is a custom header, so any cross-site call triggers a
  CORS preflight the plugin does not answer — third-party pages cannot drive the
  proxy. Vouch's own server needs no CORS changes.

**Connection model:**

- First run (or explicit disconnect): a **connect dialog** collects endpoint URL
  (default `http://127.0.0.1:8731`) and optional bearer token. Stored in
  `localStorage`.
- Validation on connect and on app load: `GET /proxy/health` and
  `GET /proxy/capabilities` (both routed through the proxy with the target header).
- The `/capabilities` response's `methods` array **capability-gates** the UI —
  views and actions whose backing method is not advertised render disabled
  (e.g. no `kb.approve` → Review is read-only). Same pattern as vouch-desktop.

**Client layers:**

- `rpc<T>(method, params)` — one typed wrapper over the envelope. Generates request
  ids, attaches bearer + target headers, unwraps `result`, throws a typed
  `VouchRpcError { code, message }` on `ok: false`.
- **TanStack Query** for all server state: caching, background refetch of the
  pending queue and health, cache invalidation after approve/reject.
- **React Router** with a persistent sidebar layout: `/chat`, `/review`, `/browse`
  (+ `/browse/:kind/:id` detail), `/stats`.
- React context for the connection (endpoint, token, capabilities, health status).

## 2. Views & chat behavior

### Chat (default route)

- Message-thread UI. Submitting a query calls `kb.synthesize { query, depth, max_chars }`.
- The answer is deterministic citation-bearing prose: sentences of the form
  `<clause> [<claim-id>].`. The renderer parses `[claim-id]` tokens into
  **clickable citation chips**; clicking opens a claim drawer backed by
  `kb.read_claim`, `kb.cite`, and `kb.why`.
- Below each answer: a confidence badge (`_meta.synthesis_confidence`:
  low/medium/high) and, when present, `gaps` rendered as
  "no approved claim covers: <term>, …".
- `/search <query>` slash command (also a mode toggle next to the input) runs
  `kb.search { query, limit }` instead and renders **hit cards**
  (kind, id, snippet, score, backend badge); clicking a hit opens the same
  detail drawer.
- Thread history persists in `localStorage`, keyed by endpoint.
- Empty-KB and empty-answer states explain what to do next (e.g. "no approved
  claims yet — propose knowledge from your agent, then approve it in Review").

### Review

- Queue list from `kb.list_pending` (auto-refresh via TanStack Query polling).
- Detail pane renders the proposal payload (kind, body, citations, session).
- **Approve** / **Reject** → `kb.approve` / `kb.reject`; optimistic removal from
  the queue; invalidates Browse and Stats caches.

### Browse

- Tabs: Claims / Pages / Entities / Relations → `kb.list_claims`,
  `kb.list_pages`, `kb.list_entities`, `kb.list_relations`.
- Client-side text filter over the loaded list.
- Row click → detail panel: the artifact rendered nicely (not raw JSON dump),
  citations via `kb.cite`, and for claims the `kb.why` provenance
  (session, sources, review trail).

### Stats

- Tiles: artifact counts + pending queue depth (`kb.status`), review-rate
  metrics (`kb.stats`), server health (from `/health` polling), and a
  capabilities card (server name, level, method count from `/capabilities`).

### Out of scope for v1

Graph explorer, propose-* forms (vouch-desktop covers data entry), dual-solve,
multi-KB switching, LLM-backed synthesis (vouch's synthesize is deterministic;
`llm: true` is not configured server-side).

## 3. Look & feel

- Dark-first, os.agno.com-inspired: near-black canvas, hairline borders, a single
  accent color, compact icon sidebar (left), top bar with a **connection pill**
  (endpoint host + live green/red health dot).
- Monospace for artifact ids; generous, instructive empty states.
- Light theme supported from day one via CSS variables consumed by Tailwind.
- The `frontend-design` skill is applied during implementation so the result
  reads as intentional design, not a default component-library template.

## 4. Data flow & error handling

- All server interaction flows through the single `rpc` client → proxy → vouch.
- Envelope errors surface **inline where they occur** (an error bubble in chat,
  an error card in Review) plus a transient toast; every error shows
  `code: message`.
- HTTP 401 from the proxy target reopens the connect dialog, pre-filled with the
  saved endpoint, prompting for a (new) token.
- Network / connection-refused errors flip the connection pill red and show a
  reconnect banner; TanStack Query retries with backoff.
- Every list view has a designed empty state (fresh KB is the common case).

## 5. Testing

- **Vitest + React Testing Library**:
  - citation parser (`[claim-id]` extraction, edge cases: adjacent citations,
    ids containing dashes, no-citation answers)
  - `rpc` client (mocked `fetch`: envelope unwrap, error mapping, headers)
  - chat flow (query → synthesize render, gaps, confidence, `/search` command)
  - review flow (approve/reject with mocked rpc, optimistic update)
- **Proxy plugin unit test**: target-header validation (rejects non-http(s),
  missing header), path rewrite.
- **Playwright smoke e2e**: script creates a temp KB (`vouch init`), seeds and
  approves a claim, starts `vouch serve --transport http` on a free port, then
  walks connect → chat question → citation chip → review queue end-to-end.
