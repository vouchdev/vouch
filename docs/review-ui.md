# `vouch review-ui` — browser-based review console

The review gate is vouch's load-bearing primitive, but the terminal
(`vouch pending`, `vouch approve <id>`) only scales to a solo reviewer and a
small queue. `vouch review-ui` adds a browser viewport over the **same**
review surface — every approve/reject/contradict goes through the identical
`vouch.proposals` / `vouch.lifecycle` code path as the CLI, so the audit log is
the same regardless of which surface you used. The CLI is untouched.

Zero new on-disk schema. Zero new `kb.*` RPC methods. The web layer reads and
mutates through the existing `KBStore`.

## Run it

```bash
vouch review-ui                              # 127.0.0.1:7780, opens browser
vouch review-ui --bind 127.0.0.1:8000
vouch review-ui --no-open-browser            # ssh / headless friendly

# team mode — a non-loopback bind REQUIRES a Bearer token:
vouch review-ui --bind 0.0.0.0:7780 --auth generate          # mints + prints a token
VOUCH_REVIEW_TOKEN=… vouch review-ui --bind 0.0.0.0:7780 --auth env
vouch review-ui --bind 0.0.0.0:7780 --auth my-shared-secret --reviewer alice
```

A non-loopback bind without `--auth` is refused outright — we won't expose an
unauthenticated approve surface on the network.

## Authentication

When `--auth` is set, every route except `/healthz` and `/static` requires the
token. Credentials are accepted in three places, in priority order:

1. **`Authorization: Bearer <token>`** header — for the CLI, scripts, and API
   callers.
2. **HttpOnly cookie** (`vouch_review_token`) — the steady-state browser path.
3. **`?token=` query string** — a *one-time bootstrap* only. On a `GET`, a
   valid query token is moved into an HttpOnly, `SameSite=Strict` cookie and
   the request is `303`-redirected to the same path with `?token=` stripped,
   so the bare token never lingers in a bookmarkable URL or in access logs.

The token is never exposed to JavaScript (the cookie is HttpOnly), so an XSS
can't read it. Token comparisons are constant-time (`secrets.compare_digest`)
to avoid a timing oracle. `secure` is not set on the cookie by default so the
localhost-first (plain-http) flow works; terminate TLS at a proxy and have the
proxy mark the cookie `Secure` for an internet-facing deployment.

## Install

The web stack lives behind an optional extra so the base install stays light:

```bash
pip install 'vouch-kb[web]'
```

All HTML/CSS/JS ships **inside the wheel** — no `npm install`, no build step,
no CDN.

## Views

| Route | What it shows |
|-------|---------------|
| `/` | the pending queue, server-side paginated |
| `/claim/<id>` | one proposal's full payload + approve/reject |
| `/session/<id>` | every proposal from one agent run, grouped, with status |
| `/sources/<id>` | reverse index: which durable claims cite this source |
| `/audit` | the review-decision timeline |
| `/api/pending?page=N` | machine-readable paginated queue (`{count,page,pages,items}`) |
| `/healthz` | liveness + pending count + connected client count (always open) |
| `/ws` | realtime channel (see below) |

## Realtime sync

A single WebSocket channel per KB (`/ws`) keeps two reviewers in sync: when a
mutation lands, the handling route broadcasts a small `{"type":"refresh"}`
frame and every connected browser re-pulls the affected view within a second.
The frame is a *signal*, not data — the client re-fetches through the same
paginated routes, so there's exactly one rendering path. With `--auth` on, the
socket authenticates on the same-origin HttpOnly cookie the browser sends with
the handshake (a `?token=` query param is also accepted for non-browser
clients like the CLI).

## Progressive enhancement

Every action is a plain `<form method=post>`, so the gate works with
**JavaScript disabled** — you can read claims and approve/reject without it.
The WebSocket live-refresh and the keyboard shortcuts (`j`/`k` to move, `a`
to approve, `r` to focus the reject reason, `?` for help) are an additive
layer on top.

## Performance

The queue is paginated at the storage layer: only the requested page of
proposal files is parsed, not the whole queue. A 500-item queue's first page
renders in well under the 200 ms budget (~30 ms locally) because the other 450
files are never deserialised for that request.

## Out of scope

- Hosted "vouch cloud" — this is local/self-hosted only.
- Free-form claim editing in the browser; the gate is approve / reject /
  contradict.
- Deleting durable claims from the UI — that stays CLI-only so a misclick
  can't blow away history.
- Auth beyond Bearer (OAuth/SSO can layer on later).
