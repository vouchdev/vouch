# vouch demo — try it in one command

A self-contained Docker demo of [vouch](https://github.com/vouchdev/vouch), the
git-native, **review-gated** knowledge base for LLM agents. One image bundles
the vouch server and the vouch-ui web console, and seeds a starter knowledge
base on first run — so you open the browser and immediately have something to
explore.

## Run it (no clone needed)

One command pulls the published image and starts everything:

```bash
docker run --rm -p 127.0.0.1:5173:5173 -v vouch-demo-data:/data \
  ghcr.io/plind-junior/vouch-demo
```

Then open **http://localhost:5173** and connect with the pre-filled endpoint.

That's it. The first run seeds a starter KB (a claim, a page, a source), so the
console opens onto a populated, review-gated knowledge base — not an empty one.
Your data persists in the `vouch-demo-data` volume between runs; `Ctrl-C` stops
the demo (the `--rm` removes only the container, never the volume).

## Update to the latest

The image is updated in place, so pulling gets you the newest build (and any
fixes). Stop the demo, then:

```bash
docker pull ghcr.io/plind-junior/vouch-demo   # fetch the latest image
```

Re-run the command from "Run it" — Docker now starts the updated image, and
your `vouch-demo-data` volume carries over. To start completely fresh instead,
reset the data with `docker volume rm vouch-demo-data` before running.

## Build from source (to hack on it)

Working in a clone of this repo? Build the image from the checkout instead of
pulling — it picks up your local changes to `webapp/` and `src/`:

```bash
cd demo
cp .env.example .env      # optional: edit VOUCH_HTTP_TOKEN, add ANTHROPIC_API_KEY
docker compose up --build
```

## Turn on the LLM features (optional)

Two console actions call a language model: **Compile** (turn approved claims
into topic pages) and **Summarize session**. They're off by default because
the demo ships no API key. Everything else — browsing, propose → approve,
delete / archive / supersede, clear queue — works without one.

To switch them on, give the demo *your own* Anthropic key. With the pulled
image, pass it in with `-e`:

```bash
docker run --rm -p 127.0.0.1:5173:5173 -v vouch-demo-data:/data \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  ghcr.io/plind-junior/vouch-demo
```

Building from source? Put it in `.env` instead:

```bash
cp .env.example .env      # then set ANTHROPIC_API_KEY=sk-ant-...
docker compose up --build
```

The key never reaches the browser — vouch runs a tiny stdlib shim
(`vouch-llm`) inside the container that calls the Anthropic Messages API
directly. Override the model with `ANTHROPIC_MODEL` if you want a newer Sonnet.
Without a key, Compile / Summarize simply report "not configured" — that's the
review gate telling you the step is unavailable, not a crash.

## What you can do in the console

- **Browse / Claims** — the seeded knowledge, with citations and provenance
  ("why does this claim exist?").
- **Pending** — the propose → approve review gate, made visible.
- **Delete / Archive / Supersede** — retire a claim through the gate: delete
  files a review-gated proposal (refused if other pages still cite it, which is
  the point), archive hides it from retrieval, supersede replaces it.
- **Clear queue** — reject the whole pending queue at once.

## How it works

One container runs two processes (managed by `entrypoint.sh`):

1. `vouch serve --transport http` on `127.0.0.1:8731` inside the container, with
   a bearer token.
2. the console via `vite preview`, whose `/proxy/*` middleware forwards to the
   in-container vouch endpoint. The token is **pinned and injected server-side**
   (`VOUCH_TARGET` / `VOUCH_HTTP_TOKEN`), so the browser never sees it.

Only the console port is published, and only on `127.0.0.1` — nothing leaves
your machine. Your KB lives in the `vouch-demo-data` volume:

```bash
# pulled image (docker run):
Ctrl-C                              # stop (keeps your data)
docker volume rm vouch-demo-data    # reset the demo KB

# built from source (docker compose):
docker compose down                 # stop (keeps your data)
docker compose down -v              # stop and reset the demo KB
```

## Notes

- The published image (`ghcr.io/plind-junior/vouch-demo`) carries the newest
  `kb.*` surface, including delete / archive / supersede — a released `vouch`
  from PyPI or `ghcr.io/vouchdev/vouch` would not yet advertise those. Building
  from source picks up whatever is in your checkout.
- The LLM-backed actions (Compile, Summarize session) need an
  `ANTHROPIC_API_KEY` — see "Turn on the LLM features" above. The rest of the
  console is fully functional without one.
- The console's "Chat / Claude Code" mode is a dev-server feature and is not
  wired up in this preview build; everything else works.
