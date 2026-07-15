#!/usr/bin/env bash
#
# Run the vouch backend and the vouch-ui web console together.
#
# Starts `vouch serve --transport http` (127.0.0.1:8731) and the vouch-ui Vite
# dev server (webapp/, http://localhost:5173) as a pair, and shuts both down on
# Ctrl-C. The dev server proxies the UI's /proxy/* calls to the vouch endpoint
# (via the X-Vouch-Target header), so the browser talks only to the dev server
# and vouch itself stays unmodified and needs no CORS.
#
# Usage:
#   make console                                  # from the repo root
#   scripts/console.sh                            # equivalent
#   VOUCH=vouch scripts/console.sh                # use a `vouch` on PATH
#   VOUCH_HOST=0.0.0.0 VOUCH_PORT=8731 scripts/console.sh
#
# Requires: node + npm (for the UI). Dependencies install automatically on the
# first run. Open http://localhost:5173 and connect to http://127.0.0.1:8731.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBAPP="$ROOT/webapp"
HOST="${VOUCH_HOST:-127.0.0.1}"
PORT="${VOUCH_PORT:-8731}"

# Chat's "Claude Code mode" spawns `claude -p` in a workspace; default it to the
# repo root (the vendored bridge's own default of ../vouch is wrong from webapp/).
export VOUCH_PROJECT_DIR="${VOUCH_PROJECT_DIR:-$ROOT}"

# Prefer the repo virtualenv's vouch, then one on PATH. Override with $VOUCH.
if [ -z "${VOUCH:-}" ]; then
  if [ -x "$ROOT/.venv/bin/vouch" ]; then
    VOUCH="$ROOT/.venv/bin/vouch"
  else
    VOUCH="vouch"
  fi
fi

if [ ! -d "$WEBAPP" ]; then
  echo "console: $WEBAPP not found — the webapp/ folder is missing" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "console: npm not found — install Node.js to run the web console" >&2
  exit 1
fi

# First run: install the web console's dependencies.
if [ ! -d "$WEBAPP/node_modules" ]; then
  echo "[console] installing web console dependencies (first run, may take a minute)…"
  (cd "$WEBAPP" && npm install) || { echo "console: npm install failed" >&2; exit 1; }
fi

pids=()
cleanup() {
  trap - EXIT INT TERM HUP
  echo
  echo "[console] shutting down…"
  for pid in "${pids[@]}"; do
    # Each service is started with `setsid`, so its pid is a process-group
    # leader: kill the whole group (negative pid) to take down grandchildren
    # too (e.g. npm -> vite), then fall back to the bare pid.
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

echo "[console] backend : $VOUCH serve --transport http  ->  http://$HOST:$PORT"
# shellcheck disable=SC2086
setsid $VOUCH serve --transport http --host "$HOST" --port "$PORT" &
pids+=($!)

echo "[console] frontend: vouch-ui dev server             ->  http://localhost:5173"
setsid bash -c 'cd "$1" && exec npm run dev' _ "$WEBAPP" &
pids+=($!)

echo
echo "[console] both running. Open http://localhost:5173  (endpoint http://$HOST:$PORT)"
echo "[console] press Ctrl-C to stop both."

# Return (and tear both down via the trap) as soon as either process exits.
wait -n 2>/dev/null || wait
