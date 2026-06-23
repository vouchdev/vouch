#!/usr/bin/env bash
# Capture review-ui screenshots for Flathub metainfo (#211).
#
# Prerequisites:
#   - built/installed flatpak: ./scripts/build-flatpak.sh --install
#   - a sample KB with pending proposals (or empty queue is fine for smoke)
#   - imagemagick `import` or gnome-screenshot / spectacle available
#
# Output: desktop/flatpak/screenshots/{queue,detail}.png

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/screenshots"
mkdir -p "${OUT}"

PORT="${VOUCH_SCREENSHOT_PORT:-7780}"
URL="http://127.0.0.1:${PORT}/"

echo "Starting vouch review-ui on ${URL} (flatpak)…"
flatpak run --command=vouch com.vouchdev.vouch review-ui \
  --bind "127.0.0.1:${PORT}" \
  --no-open-browser &
PID=$!
trap 'kill ${PID} 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  if curl -fsS "${URL}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if command -v import >/dev/null; then
  import -window root "${OUT}/queue.png"
else
  echo "install imagemagick or capture ${URL} manually into ${OUT}/" >&2
  exit 1
fi

echo "wrote ${OUT}/queue.png"
echo "open a proposal detail view and re-run with VOUCH_SCREENSHOT=detail for the second shot"
