# Flatpak desktop install (#211)

Linux users on Fedora, Arch, and other Flathub-enabled desktops can install the
vouch **review console** as a sandboxed Flatpak instead of pipx.

## Install (once on Flathub)

```bash
flatpak install flathub com.vouchdev.vouch
flatpak run com.vouchdev.vouch
```

Until the Flathub listing is live, build from source — see
[`desktop/flatpak/README.md`](../desktop/flatpak/README.md).

## What the package contains

The Flatpak ships `vouch review-ui`: a local FastAPI server plus browser UI for
approving or rejecting agent proposals. It does **not** replace the full CLI or
MCP server — pair it with `pipx install vouch-kb` or a host adapter for the
propose side of the loop.

## Filesystem access

v1 grants `--filesystem=home` so vouch can discover and mutate `.vouch/` trees
under your home directory (project checkouts and optional `~/.vouch/`). Narrower
per-KB permissions may come in a later release.

## Network

`--share=network` allows binding `127.0.0.1` for the review UI and loading it
in your browser. The app does not phone home.

## Packaging source

All manifests, icons, and validators live under `desktop/flatpak/`. CI runs
`tests/test_flatpak.py` on every PR to keep the manifest aligned with issue
#211 acceptance criteria.

## Related

- [Getting started](./getting-started.md) — agent + human review loop
- [GitHub issue #211](https://github.com/vouchdev/vouch/issues/211) — tracking
