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
# command in `compile.llm_cmd`. Support two workflows:
# 1. If Claude CLI is available (~/.claude exists), use `claude -p` to capture
#    in Claude sessions (same as the real vouch project).
# 2. If only ANTHROPIC_API_KEY is set, use the direct API shim (vouch-llm).
# 3. If neither, leave LLM features unset so actions return "not configured".
CONFIG="$DATA/.vouch/config.yaml"
LLM_CMD=""
if [ -d "$HOME/.claude" ]; then
  LLM_CMD="claude -p --model sonnet-4-5"
  echo "[demo] Claude CLI found — LLM features wire to 'claude -p', compile & summarize will capture in Claude sessions."
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  LLM_CMD="vouch-llm"
  echo "[demo] ANTHROPIC_API_KEY set — LLM features enabled via direct API (compile & summarize will NOT capture in Claude sessions; use 'claude login' + mount ~/.claude to enable session capture)."
else
  echo "[demo] LLM features DISABLED — provide ANTHROPIC_API_KEY or mount ~/.claude (with 'claude login' done) to enable compile & summarize."
fi

if [ -n "$LLM_CMD" ]; then
  python3 - "$CONFIG" "$LLM_CMD" <<'PY'
import sys, yaml
path, cmd = sys.argv[1:3]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
cfg.setdefault("compile", {})["llm_cmd"] = cmd
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY
else
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
