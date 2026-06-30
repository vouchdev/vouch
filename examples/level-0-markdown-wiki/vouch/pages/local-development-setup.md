---
id: local-development-setup
title: Local development setup
type: concept
status: draft
claims: []
entities: []
sources: []
tags: []
metadata: {}
created_at: '2026-06-30T02:29:38.872660Z'
updated_at: '2026-06-30T02:29:38.872667Z'
---
How to run the acme-example platform on your own machine. These are just
notes; nothing here is a reviewed claim yet.

## Prerequisites

- Python 3.11+ and a recent Node.
- Docker for the local Postgres and Redis containers.

## Steps

1. `make bootstrap` installs deps and seeds a local database.
2. `make dev` starts the API on :8080 and the web app on :3000.
3. `make test` runs the unit suite; it should be green on a clean clone.

## Gotchas

- If `make dev` fails on the database, run `docker compose down -v` and
  retry; a stale volume is the usual cause.
- The web app expects `VITE_API_URL=http://localhost:8080`.
