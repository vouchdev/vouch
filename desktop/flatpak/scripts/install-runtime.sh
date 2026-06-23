#!/usr/bin/env bash
# Install org.freedesktop.Platform//23.08 + SDK for local flatpak builds (#211).
set -euo pipefail

RUNTIME_VERSION="${FLATPAK_RUNTIME_VERSION:-23.08}"

flatpak install -y --user flathub \
  "org.freedesktop.Platform//${RUNTIME_VERSION}" \
  "org.freedesktop.Sdk//${RUNTIME_VERSION}" \
  "org.freedesktop.Sdk.Extension.python3//${RUNTIME_VERSION}" \
  "org.freedesktop.Platform.Extension.python3//${RUNTIME_VERSION}"

echo "runtime ${RUNTIME_VERSION} ready"
