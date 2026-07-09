#!/usr/bin/env bash
#
# vouch demo entrypoint: seed a starter KB on first run, start the vouch server
# on loopback inside this container, then serve the console. Both processes live
# in one container; killing the container stops both.
set -euo pipefail

DATA="${VOUCH_DATA_DIR:-/data}"
TOKEN="${VOUCH_HTTP_TOKEN:-vouch-demo}"
# actor recorded in the seeded KB's audit log (avoids getpass in a bare image)
export VOUCH_USER="${VOUCH_USER:-demo}"

# First run: an empty /data volume gets a seeded, review-gated starter KB so the
# console has something to show. Idempotent — skipped once .vouch/ exists.
if [ ! -d "$DATA/.vouch" ]; then
  echo "[demo] seeding a starter knowledge base in $DATA ..."
  vouch init --path "$DATA"
fi

# vouch's LLM features (page compile, session summaries) shell out to the
# command in `compile.llm_cmd`. Point it at the demo shim only when the user
# supplied a key — otherwise leave it unset so those actions return vouch's
# clean "not configured" message instead of a dead command. Re-run every start
# so an existing volume lights up the moment a key appears (and goes dark if it
# is removed). Everything else — browse, propose→approve, delete/archive — works
# with no key at all.
CONFIG="$DATA/.vouch/config.yaml"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  LLM_CMD="vouch-llm" python3 - "$CONFIG" <<'PY'
import os, sys, yaml
path = sys.argv[1]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
cfg.setdefault("compile", {})["llm_cmd"] = os.environ["LLM_CMD"]
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY
  echo "[demo] LLM features ENABLED — Claude via ANTHROPIC_API_KEY (model=${ANTHROPIC_MODEL:-claude-sonnet-4-5})."
else
  # Drop any llm_cmd a previous keyed run wrote, so state matches the missing key.
  python3 - "$CONFIG" <<'PY'
import sys, yaml
path = sys.argv[1]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
if isinstance(cfg.get("compile"), dict):
    cfg["compile"].pop("llm_cmd", None)
    if not cfg["compile"]:
        cfg.pop("compile")
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY
  echo "[demo] LLM features DISABLED — set ANTHROPIC_API_KEY to enable page compile & session summaries; everything else works without it."
fi

# vouch on loopback (same container). A token is set so the console's proxy can
# inject it server-side; the browser never sees it.
echo "[demo] starting vouch server on 127.0.0.1:8731 ..."
( cd "$DATA" && exec vouch serve --transport http --host 127.0.0.1 --port 8731 --token "$TOKEN" ) &
VOUCH_PID=$!
trap 'kill "$VOUCH_PID" 2>/dev/null || true' EXIT INT TERM

# Wait for vouch to answer its public liveness probe before starting the UI.
for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:8731/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "[demo] starting the vouch console on :5173 — open http://localhost:5173"
cd /app/webapp
exec npm run preview -- --host 0.0.0.0 --port 5173
