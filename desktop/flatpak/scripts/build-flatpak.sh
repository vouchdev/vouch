#!/usr/bin/env bash
# Build and optionally install the vouch Flatpak locally (#211).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${ROOT}/com.vouchdev.vouch.yaml"
BUILD_DIR="${ROOT}/build-dir"
REPO_DIR="${ROOT}/repo"

usage() {
  cat <<'EOF'
Usage: build-flatpak.sh [--install] [--run] [--clean]

  --install   install the build into the user Flatpak repo
  --run       run com.vouchdev.vouch after a successful build
  --clean     remove build-dir/ and repo/ before building
EOF
}

INSTALL=0
RUN=0
CLEAN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --install) INSTALL=1 ;;
    --run) RUN=1 ;;
    --clean) CLEAN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

command -v flatpak-builder >/dev/null || {
  echo "flatpak-builder not found; install flatpak-builder" >&2
  exit 1
}

python3 "${ROOT}/scripts/generate-icons.py"
python3 "${ROOT}/scripts/validate-manifest.py" --strict

if [ "$CLEAN" = 1 ]; then
  rm -rf "${BUILD_DIR}" "${REPO_DIR}"
fi

ARGS=(--force-clean --repo="${REPO_DIR}" "${BUILD_DIR}" "${MANIFEST}")
if [ "$INSTALL" = 1 ]; then
  ARGS=(--user --install "${ARGS[@]}")
fi

flatpak-builder "${ARGS[@]}"

if [ "$RUN" = 1 ]; then
  flatpak run com.vouchdev.vouch
fi
