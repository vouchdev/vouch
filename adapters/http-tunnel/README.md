# http-tunnel — public-internet reference deployments for `vouch serve --transport http`

`vouch serve --transport http` is loopback-only by default. To expose it to
Claude.ai Custom Connectors (or any other off-host MCP client), you need:

1. A public hostname with TLS.
2. The vouch HTTP server bound on a non-loopback host, **with at least one
   bearer token** (vouch refuses to run that combination without it — see
   `--allow-public` in `vouch serve --help`).
3. A way to keep the kb file-tree on disk reachable by the server process.

This directory ships three drop-in templates for the same outcome — pick the
one that matches your hosting:

| File | Where | What you get |
|------|-------|--------------|
| [`Dockerfile`](Dockerfile) | anywhere with a container runtime | A minimal Python image that runs `vouch serve --transport http --host 0.0.0.0 --allow-public` against the `/data/.vouch` volume. |
| [`fly.toml`](fly.toml) | fly.io | TLS-terminated public URL at `https://<your-app>.fly.dev`, the Dockerfile is built and deployed by `fly deploy`. |
| [`cloudflare-tunnel/compose.yml`](cloudflare-tunnel/compose.yml) | self-hosted | The same Dockerfile, fronted by a Cloudflare Tunnel sidecar so the vouch instance never opens an inbound port. |

Every template treats the **bearer token as the trust boundary** — there is
no public deployment of vouch without one. The CLI's own
`--allow-public + --token` (or `serve.bearer_tokens` in `config.yaml`) gate is
the last line of defence; the tunnel just makes the box reachable.

## Common setup (every deployment)

```bash
# In the directory holding your .vouch/ knowledge base:
export VOUCH_TOKEN=$(openssl rand -hex 32)
cat > .vouch/config.yaml <<EOF
serve:
  bearer_token: env:VOUCH_TOKEN
EOF
```

Then pick a template below.

## Option A — Docker (any host)

```bash
docker build -f adapters/http-tunnel/Dockerfile -t vouch-http .
docker run -d --name vouch \
  -p 8731:8731 \
  -v "$PWD/.vouch:/data/.vouch" \
  -e VOUCH_TOKEN="$VOUCH_TOKEN" \
  vouch-http
```

You're now serving on `http://localhost:8731`. To reach it from elsewhere put
nginx / caddy / Cloudflare in front for TLS; `vouch serve` does not terminate
TLS in-process (see VEP-0004 §Security model for the rationale).

## Option B — fly.io (managed TLS, one command)

```bash
cd adapters/http-tunnel
fly launch --copy-config --name my-vouch
fly secrets set VOUCH_TOKEN="$VOUCH_TOKEN"
fly volumes create vouchdata --size 1
fly deploy
```

The bundled `fly.toml` maps `/data/.vouch` into the persistent volume so the
KB survives redeploys. Public URL: `https://my-vouch.fly.dev/mcp` (Claude.ai
Custom Connector target).

## Option C — Cloudflare Tunnel (no open ports)

```bash
cd adapters/http-tunnel/cloudflare-tunnel
export VOUCH_TOKEN="$VOUCH_TOKEN"
export CLOUDFLARE_TUNNEL_TOKEN="<token from cloudflare zero-trust dashboard>"
docker compose up -d
```

The compose file runs vouch + `cloudflared` side-by-side on a private network.
`cloudflared` dials Cloudflare's edge from inside; nothing accepts inbound
traffic locally. The public hostname is whatever you set in the Cloudflare
Zero Trust dashboard when you minted the tunnel token; point it at
`http://vouch:8731` (the in-compose hostname).

## Wiring Claude.ai

1. Claude.ai → Settings → Custom Connectors → **Add Connector**
2. Server URL: `https://<your-host>/mcp` (or `/messages` — both work)
3. Auth: **Bearer**, paste your `VOUCH_TOKEN`
4. Hit "Test" — Claude's validator probes `/health` (unauthenticated) and
   then runs an MCP `initialize` handshake against `/mcp` (with the bearer).
   Both should be green.

## Wiring Claude mobile, Managed Agents, Computer Use, Messages API

All four take an `mcp_servers` list with `{"url": "...", "type": "url",
"authorization_token": "..."}`. Use the same URL + token you just gave
Claude.ai.

## Why a reference deployment lives in the repo

PRs that add a new public-internet surface to a credential-bearing service
are easier to land when the deployment scaffolding ships at the same time as
the code. Reviewers can see *exactly* how the trust boundary is meant to be
configured — there's no "and now you must do this safely off-screen" gap
where a contributor follows a Medium tutorial and ends up with an open
relay. See also #176 ("five Claude surfaces blocked") for the deployment
contexts these templates were designed for.
