#!/bin/sh
# Flatpak entrypoint for the vouch review console (#211).
#
# Runs the local uvicorn server and opens the system browser via the desktop
# portal. KB discovery uses the same rules as the CLI: nearest .vouch/ above
# cwd, or pass --kb / set VOUCH_KB_PATH.

set -eu

BIND="${VOUCH_REVIEW_BIND:-127.0.0.1:7780}"

if [ -n "${VOUCH_KB_PATH:-}" ]; then
  exec vouch review-ui --bind "${BIND}" --kb "${VOUCH_KB_PATH}" "$@"
fi

exec vouch review-ui --bind "${BIND}" "$@"
