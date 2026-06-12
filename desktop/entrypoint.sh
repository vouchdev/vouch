#! /bin/bash
# vouch review-ui desktop launcher.
#
# When the AppImage is started from a .desktop entry there is no shell
# context, no project cwd, no `.vouch/` to discover — so on first launch we
# init a KB in $HOME (or VOUCH_KB_PATH if set) and then hand off to the
# normal `vouch review-ui` command, which opens the browser to the queue.

set -e

KB_ROOT="${VOUCH_KB_PATH:-$HOME}"
PYTHON="{{ python-executable }}"

if [ ! -d "$KB_ROOT/.vouch" ]; then
    mkdir -p "$KB_ROOT"
    "$PYTHON" -I -m vouch init --path "$KB_ROOT" >/dev/null
fi

exec "$PYTHON" -I -m vouch review-ui --kb "$KB_ROOT" "$@"
